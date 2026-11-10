#!/usr/bin/env python3
"""
Compact Git history to exactly one commit per tag, in place.

Workflow (every git command is printed as ``[CMD] git ...``):

1. Baseline: commit count, tags by creation date, ``du -sh data .git``, and the
   ``<tag>^{tree}`` SHA of every tag.
2. Plumbing: ``git commit-tree`` builds a new linear chain
   ``t_0 (root) -> t_1 -> ... -> t_n`` (each commit points at the tag's original
   tree, keeps its original author/committer date, message ``Release <tag>``).
   If ``HEAD^{tree}`` differs from the last tag's tree, one extra commit
   ``WIP: changes after latest tag`` is appended for it.
3. Refs: ``git tag -f <tag> <new>`` for every tag, ``git checkout -B main <final>``.
4. Verify: every tag's tree SHA is unchanged and ``data/RELEASE_MARKER.txt`` at
   ``<tag>`` equals ``<tag>``.
5. Local GC: drop stale remote-tracking refs, expire reflogs, ``gc --aggressive``.
6. If ``origin`` exists (or ``--push``): force-push main + tags, fresh-clone
   into ``--clone-dir`` and print a before/after table.

THIS REWRITES HISTORY. All intermediate commits become unreachable and are
garbage-collected.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tag_release import REPO_ROOT, run_git  # noqa: E402

BRANCH = "main"
MARKER_PATH = "data/RELEASE_MARKER.txt"
DEFAULT_CLONE_DIR = Path("/tmp/git-tag-squash-demo-fresh-clone")


# ---------------------------------------------------------------- helpers
def git_out(*args: str, show: bool = True) -> str:
    return run_git(*args, show_output=show).stdout.strip()


def run_cmd(cmd: list[str], cwd: Path = REPO_ROOT) -> str:
    print(f"[CMD] {shlex.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    for line in (proc.stdout + proc.stderr).rstrip().splitlines():
        print(f"      {line}")
    return proc.stdout


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024  # type: ignore[assignment]
    raise AssertionError


def print_table(title: str, headers: list[str], rows: list[list[str]]) -> None:
    widths = [max(len(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))]
    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    fmt = lambda r: "| " + " | ".join(str(c).ljust(w) for c, w in zip(r, widths)) + " |"  # noqa: E731
    print(f"\n{title}\n{sep}\n{fmt(headers)}\n{sep}")
    for r in rows:
        print(fmt(r))
    print(sep)


def section(title: str) -> None:
    print(f"\n{'=' * 78}\n== {title}\n{'=' * 78}")


@dataclass
class CommitMeta:
    author_name: str
    author_email: str
    author_date: str
    committer_name: str
    committer_email: str
    committer_date: str

    @classmethod
    def of(cls, rev: str) -> "CommitMeta":
        out = git_out("log", "-1", "--format=%an%n%ae%n%aI%n%cn%n%ce%n%cI", rev)
        return cls(*out.splitlines())

    def env(self) -> dict[str, str]:
        return {
            "GIT_AUTHOR_NAME": self.author_name,
            "GIT_AUTHOR_EMAIL": self.author_email,
            "GIT_AUTHOR_DATE": self.author_date,
            "GIT_COMMITTER_NAME": self.committer_name,
            "GIT_COMMITTER_EMAIL": self.committer_email,
            "GIT_COMMITTER_DATE": self.committer_date,
        }


# ---------------------------------------------------------------- steps
def preflight() -> None:
    branch = git_out("rev-parse", "--abbrev-ref", "HEAD")
    if branch != BRANCH:
        sys.exit(f"[ERROR] must be on '{BRANCH}' (currently on '{branch}')")
    if git_out("status", "--porcelain", show=False):
        run_git("status", "--short")
        sys.exit("[ERROR] working tree is not clean; commit or stash first")


def baseline() -> tuple[int, int, list[str], dict[str, str]]:
    section("Step 1/6: pre-compaction baseline")
    count = int(git_out("rev-list", "--count", "HEAD"))
    tags = git_out("tag", "--sort=creatordate").split()
    if not tags:
        sys.exit("[ERROR] no tags found; nothing to compact against")
    run_cmd(["du", "-sh", "data", ".git"])
    run_git("count-objects", "-vH")
    git_size = dir_size(REPO_ROOT / ".git")
    trees = {t: git_out("rev-parse", f"{t}^{{tree}}") for t in tags}

    # Creation-date order must agree with history (t_{i-1} ancestor of t_i).
    # If it doesn't (e.g. clock skew), fall back to topological order on HEAD.
    in_order = all(
        run_git("merge-base", "--is-ancestor", a, b, check=False, show_output=False).returncode == 0
        for a, b in zip(tags, tags[1:])
    )
    if not in_order:
        print("[WARN] creatordate order disagrees with history; using topological order on HEAD instead")
        position = {c: i for i, c in enumerate(git_out("rev-list", "--topo-order", "--reverse", "HEAD", show=False).split())}
        commits = {t: git_out("rev-parse", f"{t}^{{commit}}", show=False) for t in tags}
        unreachable = [t for t in tags if commits[t] not in position]
        if unreachable:
            sys.exit(f"[ERROR] tags not reachable from HEAD: {', '.join(unreachable)}")
        tags.sort(key=lambda t: position[commits[t]])
        print(f"[INFO] tag order: {', '.join(tags)}")

    print_table(
        "Baseline",
        ["metric", "value"],
        [["commits on HEAD", str(count)], ["tags", str(len(tags))],
         ["data/ size", human(dir_size(REPO_ROOT / "data"))], [".git size", human(git_size)]],
    )
    print_table("Tag trees (pre-compaction)", ["tag", "tree"], [[t, trees[t]] for t in tags])
    return count, git_size, tags, trees


def rebuild(tags: list[str], trees: dict[str, str]) -> tuple[dict[str, str], str]:
    section("Step 2/6: rebuild history with git commit-tree")
    new_commits: dict[str, str] = {}
    parent: str | None = None
    for tag in tags:
        meta = CommitMeta.of(f"{tag}^{{commit}}")
        args = ["commit-tree", trees[tag], "-m", f"Release {tag}"]
        if parent:
            args[2:2] = ["-p", parent]
        parent = run_git(*args, env=meta.env()).stdout.strip()
        new_commits[tag] = parent
        print(f"[INFO] {tag}: tree {trees[tag][:12]} -> new commit {parent[:12]}")

    final = parent
    head_tree = git_out("rev-parse", "HEAD^{tree}")
    if head_tree != trees[tags[-1]]:
        print("[INFO] HEAD has changes after the latest tag; appending a WIP commit")
        meta = CommitMeta.of("HEAD")
        final = run_git(
            "commit-tree", head_tree, "-p", final, "-m", "WIP: changes after latest tag", env=meta.env()
        ).stdout.strip()
    else:
        print("[INFO] HEAD^{tree} == latest tag tree; no WIP commit needed")
    assert final is not None
    return new_commits, final


def update_refs(new_commits: dict[str, str], final: str) -> None:
    section("Step 3/6: repoint tags and main")
    for tag, commit in new_commits.items():
        run_git("tag", "-f", tag, commit)
    run_git("checkout", "-B", BRANCH, final)
    run_git("log", "--oneline", "--decorate", "--graph", "-n", "20")


def verify(tags: list[str], trees: dict[str, str], git_dir_arg: list[str] | None = None) -> None:
    pre = git_dir_arg or []
    bad = []
    rows = []
    for tag in tags:
        tree = git_out(*pre, "rev-parse", f"{tag}^{{tree}}", show=False)
        marker = run_git(*pre, "show", f"{tag}:{MARKER_PATH}", check=False, show_output=False).stdout.strip()
        ok = tree == trees[tag] and marker == tag
        rows.append([tag, tree[:12], "OK" if tree == trees[tag] else "MISMATCH", marker or "<missing>",
                     "OK" if marker == tag else "MISMATCH"])
        if not ok:
            bad.append(tag)
    print_table("Integrity check", ["tag", "tree", "tree==baseline", "RELEASE_MARKER", "marker==tag"], rows)
    if bad:
        sys.exit(f"[ERROR] integrity check failed for: {', '.join(bad)}")
    print(f"[OK] all {len(tags)} tags have identical trees and correct RELEASE_MARKER.txt")


def local_gc() -> tuple[int, int]:
    section("Step 5/6: local garbage collection")
    # Remote-tracking refs (refs/remotes/*) still point at the old history and
    # would keep every old object alive. They are re-created by the next push/fetch.
    keep = {f"refs/heads/{BRANCH}"}
    for ref in git_out("for-each-ref", "--format=%(refname)").split():
        if ref.startswith("refs/remotes/"):
            run_git("update-ref", "-d", ref)
        elif ref not in keep and not ref.startswith("refs/tags/"):
            print(f"[WARN] {ref} may keep old history alive; delete it to reclaim all space")
    run_git("reflog", "expire", "--expire=now", "--expire-unreachable=now", "--all")
    run_git("gc", "--prune=now", "--aggressive", stream=True,
            hint="repacking every object from scratch; this can take a while on a large repo")
    run_cmd(["du", "-sh", "data", ".git"])
    run_git("count-objects", "-vH")
    count = int(git_out("rev-list", "--count", "HEAD"))
    return count, dir_size(REPO_ROOT / ".git")


def push_and_clone(clone_dir: Path, tags: list[str], trees: dict[str, str]) -> tuple[int, int, str]:
    section("Step 6/6: force-push and fresh clone")
    run_git("push", "--progress", "origin", BRANCH, "--force", stream=True,
            hint="force-pushing the compacted history (small, should be quick)")
    run_git("push", "--progress", "origin", "--tags", "--force", stream=True)
    url = git_out("remote", "get-url", "origin")
    if clone_dir.exists():
        print(f"[CMD] rm -rf {shlex.quote(str(clone_dir))}")
        shutil.rmtree(clone_dir)
    # --no-local: always use the pack protocol, even for a local-path origin,
    # so only reachable objects are transferred (no-op for network URLs).
    run_git("clone", "--no-local", "--progress", url, str(clone_dir), stream=True,
            hint=f"fresh clone of {url} -> {clone_dir}")
    pre = ["-C", str(clone_dir)]
    count = int(git_out(*pre, "rev-list", "--count", "HEAD"))
    run_cmd(["du", "-sh", ".git"], cwd=clone_dir)
    verify(tags, trees, pre)
    return count, dir_size(clone_dir / ".git"), url


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--push", action="store_true", help="force-push + fresh clone (default if origin exists)")
    group.add_argument("--no-push", action="store_true", help="skip force-push and fresh clone")
    parser.add_argument("--clone-dir", type=Path, default=DEFAULT_CLONE_DIR,
                        help=f"fresh clone destination (default {DEFAULT_CLONE_DIR})")
    args = parser.parse_args()

    preflight()
    count_before, git_before, tags, trees = baseline()
    new_commits, final = rebuild(tags, trees)
    update_refs(new_commits, final)

    section("Step 4/6: verify cryptographic integrity")
    verify(tags, trees)

    count_after, git_after = local_gc()
    rows = [
        ["commits on main", f"{count_before}", f"{count_after}"],
        ["local .git size", human(git_before), human(git_after)],
    ]

    has_origin = "origin" in git_out("remote", show=False).split()
    if args.push and not has_origin:
        sys.exit("[ERROR] --push given but no 'origin' remote is configured")
    if has_origin and not args.no_push:
        clone_count, clone_size, url = push_and_clone(args.clone_dir, tags, trees)
        rows += [
            ["fresh clone commits", "-", f"{clone_count}"],
            [".git before vs fresh clone .git", human(git_before), human(clone_size)],
        ]
        print(f"[INFO] fresh clone of {url} at {args.clone_dir}")
    else:
        print("\n[INFO] skip push / fresh clone (" + ("--no-push" if args.no_push else "no 'origin' remote") + ")")

    print_table("Compaction summary", ["metric", "before", "after"], rows)
    print(f"\n[DONE] {count_before} commits -> {count_after} commits, "
          f".git {human(git_before)} -> {human(git_after)} (local)")


if __name__ == "__main__":
    main()
