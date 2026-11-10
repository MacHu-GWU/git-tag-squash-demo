#!/usr/bin/env python3
"""
Cut a release tag from ``version.txt``.

Steps:

1. Read the current version ``V`` from ``version.txt`` (e.g. ``0.1.1``).
2. Write ``V`` into ``data/RELEASE_MARKER.txt``.
3. Commit ``version.txt`` + ``data/RELEASE_MARKER.txt`` (message ``Release V``)
   if either has changes.
4. Create the lightweight tag ``V``.
5. Bump ``version.txt`` to the next micro version (``0.1.2``) and commit it as
   ``Bump version to 0.1.2`` (pass ``--no-commit-bump`` to leave it uncommitted).

Only the Python 3.12+ standard library is used.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = REPO_ROOT / "version.txt"
MARKER_FILE = REPO_ROOT / "data" / "RELEASE_MARKER.txt"
SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

EnvFactory = Callable[[], Mapping[str, str] | None]


def run_git(
    *args: str,
    env: Mapping[str, str] | None = None,
    check: bool = True,
    show_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` in the repo root, printing the command and its output."""
    cmd = ["git", *args]
    print(f"[CMD] {shlex.join(cmd)}", flush=True)
    full_env = {**os.environ, **env} if env else None
    proc = subprocess.run(
        cmd, cwd=REPO_ROOT, env=full_env, text=True, capture_output=True
    )
    if show_output:
        for stream in (proc.stdout, proc.stderr):
            for line in stream.rstrip().splitlines():
                print(f"      {line}")
    if check and proc.returncode != 0:
        sys.exit(f"[ERROR] command failed (exit {proc.returncode}): {shlex.join(cmd)}")
    return proc


def read_version() -> str:
    if not VERSION_FILE.exists():
        sys.exit(f"[ERROR] {VERSION_FILE} not found")
    version = VERSION_FILE.read_text().strip()
    if not SEMVER_RE.match(version):
        sys.exit(f"[ERROR] version.txt does not hold X.Y.Z: {version!r}")
    return version


def bump_micro(version: str) -> str:
    major, minor, micro = SEMVER_RE.match(version).groups()  # type: ignore[union-attr]
    return f"{major}.{minor}.{int(micro) + 1}"


def tag_exists(tag: str) -> bool:
    proc = run_git("rev-parse", "-q", "--verify", f"refs/tags/{tag}", check=False, show_output=False)
    return proc.returncode == 0


def tag_release(env_factory: EnvFactory = lambda: None, commit_bump: bool = True) -> str:
    """Cut the release for the version in version.txt; return that version.

    ``env_factory`` is called once per commit to supply extra env vars
    (e.g. ``GIT_AUTHOR_DATE`` / ``GIT_COMMITTER_DATE`` from the simulator).
    """
    version = read_version()
    print(f"\n=== Release {version} ===")
    if tag_exists(version):
        sys.exit(f"[ERROR] tag {version} already exists")

    MARKER_FILE.parent.mkdir(parents=True, exist_ok=True)
    MARKER_FILE.write_text(f"{version}\n")
    print(f"[INFO] wrote {MARKER_FILE.relative_to(REPO_ROOT)} = {version!r}")

    paths = ["version.txt", "data/RELEASE_MARKER.txt"]
    run_git("add", "--", *paths)
    staged = run_git("diff", "--cached", "--quiet", "--", *paths, check=False, show_output=False)
    if staged.returncode == 1:
        run_git("commit", "-m", f"Release {version}", "--", *paths, env=env_factory())
    else:
        print("[INFO] version.txt / RELEASE_MARKER.txt unchanged, nothing to commit")

    run_git("tag", version)

    next_version = bump_micro(version)
    VERSION_FILE.write_text(f"{next_version}\n")
    print(f"[INFO] bumped version.txt: {version} -> {next_version}")
    if commit_bump:
        run_git("add", "--", "version.txt")
        run_git("commit", "-m", f"Bump version to {next_version}", "--", "version.txt", env=env_factory())
    return version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--no-commit-bump",
        action="store_true",
        help="leave the bumped version.txt uncommitted in the working tree",
    )
    args = parser.parse_args()
    tag_release(commit_bump=not args.no_commit_bump)


if __name__ == "__main__":
    main()
