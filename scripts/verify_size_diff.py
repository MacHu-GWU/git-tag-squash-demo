#!/usr/bin/env python3
"""
End-to-end benchmark: repo and fresh-clone size before vs. after compaction.

1. Baseline clone of the bloated state (from ``origin``, or from the local repo
   via ``file://`` if no remote is configured) into ``--baseline-dir``.
2. Run ``scripts/compact_by_tags.py`` (compaction, GC, force-push, fresh clone).
3. Fresh clone of the compacted state into ``--clone-dir``.
4. Check every tag's tree SHA is identical in both clones, then print a
   before/after table (commits, tags, local ``.git``, clone ``.git``).

Clones use ``--no-local`` so they go through the pack protocol just like a
network clone; their size is what a real ``git clone`` would download.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compact_by_tags import DEFAULT_CLONE_DIR, dir_size, git_out, human, print_table, section  # noqa: E402
from tag_release import REPO_ROOT, run_git  # noqa: E402

DEFAULT_BASELINE_DIR = Path("/tmp/git-tag-squash-demo-baseline-clone")


@dataclass
class CloneStats:
    commits: int
    tags: list[str]
    trees: dict[str, str]
    git_size: int
    data_size: int


def clone(url: str, dest: Path) -> CloneStats:
    if dest.exists():
        print(f"[CMD] rm -rf {dest}")
        shutil.rmtree(dest)
    run_git("clone", "--no-local", url, str(dest))
    pre = ["-C", str(dest)]
    tags = git_out(*pre, "tag", "--sort=creatordate").split()
    return CloneStats(
        commits=int(git_out(*pre, "rev-list", "--count", "HEAD")),
        tags=tags,
        trees={t: git_out(*pre, "rev-parse", f"{t}^{{tree}}", show=False) for t in tags},
        git_size=dir_size(dest / ".git"),
        data_size=dir_size(dest / "data"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--baseline-dir", type=Path, default=DEFAULT_BASELINE_DIR)
    parser.add_argument("--clone-dir", type=Path, default=DEFAULT_CLONE_DIR)
    parser.add_argument("--push-baseline", action="store_true",
                        help="push local main + tags to origin first if origin is behind")
    parser.add_argument("--skip-compact", action="store_true", help="only measure the baseline clone")
    args = parser.parse_args()

    has_origin = "origin" in git_out("remote", show=False).split()
    url = git_out("remote", "get-url", "origin") if has_origin else f"file://{REPO_ROOT}"

    section("Baseline: clone the bloated state")
    if has_origin:
        head = git_out("rev-parse", "HEAD")
        remote_line = git_out("ls-remote", "origin", "refs/heads/main")
        remote_head = remote_line.split()[0] if remote_line else ""
        if remote_head != head:
            if not args.push_baseline:
                sys.exit(f"[ERROR] origin/main ({remote_head[:12] or 'missing'}) != local HEAD ({head[:12]}); "
                         "re-run with --push-baseline to push the bloated state first")
            run_git("push", "origin", "main", "--force")
            run_git("push", "origin", "--tags", "--force")
    local_before = dir_size(REPO_ROOT / ".git")
    before = clone(url, args.baseline_dir)
    if args.skip_compact:
        print_table("Baseline", ["metric", "value"], [
            ["commits", str(before.commits)], ["tags", str(len(before.tags))],
            ["local .git", human(local_before)], ["clone .git", human(before.git_size)],
            ["clone data/", human(before.data_size)]])
        return

    section("Compaction: scripts/compact_by_tags.py")
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / "compact_by_tags.py"), "--clone-dir", str(args.clone_dir)]
    if not has_origin:
        cmd.append("--no-push")
    print(f"[CMD] {' '.join(cmd)}", flush=True)
    if subprocess.run(cmd, cwd=REPO_ROOT).returncode != 0:
        sys.exit("[ERROR] compact_by_tags.py failed")

    section("After: fresh clone of the compacted state")
    local_after = dir_size(REPO_ROOT / ".git")
    after = clone(url, args.clone_dir)

    mismatched = [t for t in before.tags if after.trees.get(t) != before.trees[t]]
    if mismatched or before.tags != after.tags:
        sys.exit(f"[ERROR] tag trees differ between clones: {mismatched or 'tag sets differ'}")
    print(f"[OK] all {len(after.tags)} tags have identical tree SHAs in baseline and fresh clone")

    pct = lambda a, b: f"{100 * (b - a) / a:+.1f}%" if a else "n/a"  # noqa: E731
    print_table("Before vs. after compaction", ["metric", "before", "after", "change"], [
        ["commits on main", str(before.commits), str(after.commits), pct(before.commits, after.commits)],
        ["tags", str(len(before.tags)), str(len(after.tags)), "same trees"],
        ["local .git", human(local_before), human(local_after), pct(local_before, local_after)],
        ["fresh clone .git", human(before.git_size), human(after.git_size), pct(before.git_size, after.git_size)],
        ["working tree data/", human(before.data_size), human(after.data_size), "unchanged"],
    ])
    print(f"\n[INFO] clone source: {url}")


if __name__ == "__main__":
    main()
