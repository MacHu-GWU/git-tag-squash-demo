#!/usr/bin/env python3
"""
Simulate R release cycles of realistic line-level churn under ``data/``.

For every cycle (target version ``V`` read from ``version.txt``):

- New files ``data/{V}-v0001.txt``, ``data/{V}-v0002.txt``, ... are created
  over the course of the cycle (JSONL customer records, 200-350 lines each).
- Each of the ``N`` intermediate commits edits 1-2 files of the current
  version and 2-3 files of older versions *by line slices*: insert 50-150
  lines in the middle, delete 40-140 lines at a line range, mutate 20-30
  lines in place.
- Each commit also inserts a large block of high-entropy
  ``staging_buffer`` lines (default 150-350 KB), and later commits trim most of them back out.
  The last commit of a cycle removes all of them, so the tagged tree stays
  small while ``.git`` history grows large.
- Then the release is cut (see ``tag_release.py``): marker, commit, tag, bump.

At the end the branch and tags are pushed to ``origin`` (if configured).

Commit timestamps are synthetic and strictly increasing (one tick per commit)
so ``git tag --sort=creatordate`` is unambiguous.
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tag_release import REPO_ROOT, read_version, run_git, tag_exists, tag_release  # noqa: E402

DATA_DIR = REPO_ROOT / "data"
STAGING_PREFIX = '{"type":"staging_buffer"'
SOFT_MAX_RECORDS = 350  # deletions grow when a file exceeds this many lines
MIN_LINES = 150  # deletions never shrink a file below this

FIRST_NAMES = ["Ava", "Liam", "Mia", "Noah", "Emma", "Ethan", "Sofia", "Lucas", "Chloe", "Mason",
               "Zoe", "Leo", "Aria", "Kai", "Nora", "Ivy", "Owen", "Ella", "Ryan", "Luna"]
LAST_NAMES = ["Chen", "Smith", "Garcia", "Kim", "Patel", "Nguyen", "Brown", "Wang", "Lopez", "Silva",
              "Muller", "Rossi", "Sato", "Khan", "Novak", "Ivanov", "Dubois", "Haddad", "Okafor", "Berg"]
CITIES = ["Seattle", "Austin", "Boston", "Denver", "Chicago", "Toronto", "Berlin", "Tokyo", "Lisbon", "Sydney"]
STATUSES = ["active", "pending", "suspended", "churned", "trial", "vip"]
PLANS = ["free", "starter", "pro", "business", "enterprise"]


class Gen:
    """Deterministic high-entropy line generator."""

    def __init__(self, rng: random.Random):
        self.rng = rng

    def uuid(self) -> str:
        return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))

    def hexdigest(self, nbytes: int = 32) -> str:
        return self.rng.randbytes(nbytes).hex()

    def record(self, version: str, ts: str) -> str:
        rng = self.rng
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        return json.dumps(
            {
                "id": self.uuid(),
                "name": f"{first} {last}",
                "email": f"{first.lower()}.{last.lower()}{rng.randint(1, 9999)}@example.com",
                "city": rng.choice(CITIES),
                "plan": rng.choice(PLANS),
                "status": rng.choice(STATUSES),
                "balance": round(rng.uniform(0, 25_000), 2),
                "updated_at": ts,
                "release": version,
                "sha1": self.hexdigest(20),
            },
            separators=(",", ":"),
        )

    def mutate(self, line: str, version: str, ts: str) -> str:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return self.record(version, ts)
        obj["status"] = self.rng.choice(STATUSES)
        obj["balance"] = round(self.rng.uniform(0, 25_000), 2)
        obj["updated_at"] = ts
        obj["sha1"] = self.hexdigest(20)
        return json.dumps(obj, separators=(",", ":"))

    def staging_line(self, ts: str) -> str:
        payload = base64.b64encode(self.rng.randbytes(192)).decode()
        return f'{STAGING_PREFIX},"batch":"{self.uuid()}","ts":"{ts}","payload":"{payload}"}}'


def is_staging(line: str) -> bool:
    return line.startswith(STAGING_PREFIX)


def read_lines(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n")


@dataclass
class EditStats:
    inserted: int = 0
    deleted: int = 0
    mutated: int = 0
    staging_added: int = 0
    staging_trimmed: int = 0

    def __str__(self) -> str:
        parts = [f"+{self.inserted} inserted", f"-{self.deleted} deleted", f"~{self.mutated} mutated"]
        if self.staging_added or self.staging_trimmed:
            parts.append(f"staging +{self.staging_added}/-{self.staging_trimmed}")
        return ", ".join(parts)


class Simulator:
    def __init__(self, rng: random.Random, tick_seconds: int, start_epoch: int):
        self.rng = rng
        self.gen = Gen(rng)
        self.tick = tick_seconds
        self.clock = start_epoch
        self.staging_files: set[Path] = set()

    # ---------------------------------------------------------------- time
    def next_env(self) -> dict[str, str]:
        self.clock += self.tick
        iso = datetime.fromtimestamp(self.clock, tz=timezone.utc).isoformat()
        return {"GIT_AUTHOR_DATE": iso, "GIT_COMMITTER_DATE": iso}

    def now_iso(self) -> str:
        return datetime.fromtimestamp(self.clock, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ---------------------------------------------------------------- edits
    def trim_staging(self, lines: list[str], keep_ratio: float) -> tuple[list[str], int]:
        idx = [i for i, line in enumerate(lines) if is_staging(line)]
        keep = set(self.rng.sample(idx, int(len(idx) * keep_ratio)))
        out = [line for i, line in enumerate(lines) if not is_staging(line) or i in keep]
        return out, len(lines) - len(out)

    def create_file(self, path: Path, version: str) -> int:
        ts = self.now_iso()
        lines = [self.gen.record(version, ts) for _ in range(self.rng.randint(200, 350))]
        write_lines(path, lines)
        return len(lines)

    def edit_file(self, path: Path, version: str, bloat_bytes: int, keep_ratio: float) -> EditStats:
        rng, ts, st = self.rng, self.now_iso(), EditStats()
        lines = read_lines(path)

        # 1. trim most of the previously inserted staging block(s)
        lines, st.staging_trimmed = self.trim_staging(lines, keep_ratio)

        # 2. delete a contiguous line range (40-140 lines, more if the file grew too big)
        n_del = rng.randint(40, 140) + max(0, len(lines) - SOFT_MAX_RECORDS)
        n_del = max(0, min(n_del, len(lines) - MIN_LINES))
        if n_del:
            start = rng.randint(0, len(lines) - n_del)
            del lines[start : start + n_del]
        st.deleted = n_del

        # 3. insert 50-150 new lines in the middle of the file
        n_ins = rng.randint(50, 150)
        pos = rng.randint(len(lines) // 4, max(len(lines) // 4, 3 * len(lines) // 4))
        lines[pos:pos] = [self.gen.record(version, ts) for _ in range(n_ins)]
        st.inserted = n_ins

        # 4. mutate 20-30 existing record lines in place
        candidates = [i for i, line in enumerate(lines) if not is_staging(line)]
        for i in rng.sample(candidates, min(rng.randint(20, 30), len(candidates))):
            lines[i] = self.gen.mutate(lines[i], version, ts)
            st.mutated += 1

        # 5. temporarily insert a large high-entropy staging block
        if bloat_bytes:
            n = max(1, bloat_bytes // len(self.gen.staging_line(ts)))
            pos = rng.randint(0, len(lines))
            lines[pos:pos] = [self.gen.staging_line(ts) for _ in range(n)]
            st.staging_added = n

        write_lines(path, lines)
        if any(is_staging(line) for line in lines):
            self.staging_files.add(path)
        else:
            self.staging_files.discard(path)
        return st

    def trim_only(self, path: Path, keep_ratio: float) -> int:
        lines, trimmed = self.trim_staging(read_lines(path), keep_ratio)
        write_lines(path, lines)
        if not any(is_staging(line) for line in lines):
            self.staging_files.discard(path)
        return trimmed

    # ---------------------------------------------------------------- cycle
    def run_cycle(self, cycle_no: int, n_commits: int, files_per_release: int,
                  bloat_min_kb: int, bloat_max_kb: int) -> str:
        rng = self.rng
        version = read_version()
        if tag_exists(version):
            sys.exit(f"[ERROR] tag {version} already exists; refusing to simulate it again")
        print(f"\n{'#' * 78}\n# Release cycle {cycle_no}: target version {version} ({n_commits} commits)\n{'#' * 78}")

        older_files = sorted(p for p in DATA_DIR.glob("*-v*.txt") if not p.name.startswith(f"{version}-v"))
        current_files: list[Path] = sorted(DATA_DIR.glob(f"{version}-v*.txt"))
        # commit indices (1-based) at which a new versioned file is created
        create_at = {1 + (k * n_commits) // files_per_release for k in range(files_per_release)}

        for i in range(1, n_commits + 1):
            last = i == n_commits
            keep_ratio = 0.0 if last else rng.uniform(0.0, 0.15)
            print(f"\n--- [{version}] commit {i}/{n_commits} ---")
            touched: list[str] = []

            if i in create_at:
                path = DATA_DIR / f"{version}-v{len(current_files) + 1:04d}.txt"
                n = self.create_file(path, version)
                current_files.append(path)
                print(f"  + create {path.relative_to(REPO_ROOT)} ({n} lines)")
                touched.append(path.name)

            targets = rng.sample(current_files, min(rng.randint(1, 2), len(current_files)))
            targets += rng.sample(older_files, min(rng.randint(2, 3), len(older_files)))
            bloat_target = None if last else targets[0]

            for path in targets:
                bloat = rng.randint(bloat_min_kb, bloat_max_kb) * 1024 if path == bloat_target else 0
                st = self.edit_file(path, version, bloat, keep_ratio)
                print(f"  ~ edit   {path.relative_to(REPO_ROOT)}: {st}")
                touched.append(path.name)

            for path in sorted(self.staging_files - set(targets)):
                trimmed = self.trim_only(path, keep_ratio)
                print(f"  - trim   {path.relative_to(REPO_ROOT)}: staging -{trimmed}")
                touched.append(path.name)

            run_git("add", "--", "data")
            msg = f"[{version}] churn {i}/{n_commits}: update {len(set(touched))} data files"
            if last:
                msg += " (drop staging buffers)"
            run_git("commit", "-m", msg, env=self.next_env())

        leftover = [p for p in DATA_DIR.glob("*.txt") if any(is_staging(x) for x in read_lines(p))]
        if leftover:
            sys.exit(f"[ERROR] staging lines left before tagging: {leftover}")
        return tag_release(env_factory=self.next_env)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-r", "--releases", type=int, default=5, help="number of release cycles (default 5)")
    parser.add_argument("-n", "--commits", type=int, default=25, help="intermediate commits per cycle (default 25)")
    parser.add_argument("--files-per-release", type=int, default=4, help="new data files per cycle (default 4)")
    parser.add_argument("--bloat-min-kb", type=int, default=150, help="min staging block per commit, KB (default 150)")
    parser.add_argument("--bloat-max-kb", type=int, default=350, help="max staging block per commit, KB (default 350)")
    parser.add_argument("--seed", type=int, default=None, help="RNG seed (default: random)")
    parser.add_argument("--no-push", action="store_true", help="do not push to origin at the end")
    parser.add_argument("--force-push", action="store_true", help="push with --force (e.g. origin already compacted)")
    args = parser.parse_args()
    if args.commits < 1 or args.releases < 1 or args.files_per_release < 1:
        sys.exit("[ERROR] --releases, --commits and --files-per-release must be >= 1")

    if run_git("status", "--porcelain", show_output=False).stdout.strip():
        run_git("status", "--short")
        sys.exit("[ERROR] working tree is not clean; commit or stash first")
    DATA_DIR.mkdir(exist_ok=True)

    seed = args.seed if args.seed is not None else random.randrange(2**32)
    print(f"[INFO] seed = {seed}")
    rng = random.Random(seed)

    # Strictly increasing synthetic timestamps between HEAD's date and now.
    # Start after both HEAD and the newest existing tag (an earlier run's synthetic
    # dates may lie slightly in the future), so creatordate stays monotonic across runs.
    head_ts = int(run_git("log", "-1", "--format=%ct", show_output=False).stdout.strip() or 0)
    tag_dates = run_git("for-each-ref", "--format=%(creatordate:unix)", "refs/tags", show_output=False).stdout.split()
    head_ts = max([head_ts, *map(int, tag_dates)])
    total_commits = args.releases * (args.commits + 2)
    now = int(time.time())
    tick = max(1, min(3600, (now - head_ts) // (total_commits + 1)))
    sim = Simulator(rng, tick_seconds=tick, start_epoch=max(head_ts, now - tick * (total_commits + 1)))

    released = [
        sim.run_cycle(c, args.commits, args.files_per_release, args.bloat_min_kb, args.bloat_max_kb)
        for c in range(1, args.releases + 1)
    ]
    print(f"\n[INFO] released tags: {', '.join(released)}")
    run_git("rev-list", "--count", "HEAD")

    remotes = run_git("remote", show_output=False).stdout.split()
    if args.no_push or "origin" not in remotes:
        print("[INFO] skip push (" + ("--no-push" if args.no_push else "no 'origin' remote") + ")")
        return
    force = ["--force"] if args.force_push else []
    run_git("push", "origin", "main", *force)
    run_git("push", "origin", "--tags", *force)


if __name__ == "__main__":
    main()
