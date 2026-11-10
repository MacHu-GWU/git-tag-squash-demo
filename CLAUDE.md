# CLAUDE.md - `git-tag-squash-demo`

## 1. Purpose of This Repository

This repository (`git-tag-squash-demo`) is a testbed for simulating a plain-text data warehouse hosted on GitHub (storing structured and unstructured customer/data files) and testing a **tag-based Git history compaction workflow**.

Over time, frequent line-level additions, deletions, and edits across plain-text files under `data/` generate hundreds of intermediate commits that bloat the `.git` directory (e.g., 20 MB - 50 MB+ of history), while the actual working tree at any given release remains small (e.g., 1 MB - 2 MB). We do not care about the intermediate commits between releases—we only care about preserving the exact file tree snapshot at each Git tag.

Your goal is to build a reproducible simulation and compaction toolkit in Python (using only the Python 3.12+ standard library, zero third-party dependencies) orchestrated via `mise.toml`.

---

## 2. Hardcoded Repository Layout & Conventions

Use these exact paths and filenames—do not invent alternative names:

```text
git-tag-squash-demo/
├── CLAUDE.md
├── mise.toml
├── version.txt                     # Single line holding the current semantic version, e.g., "0.1.1\n"
├── data/
│   ├── RELEASE_MARKER.txt          # Hardcoded special file! At tag X.Y.Z, its exact content MUST be "X.Y.Z\n"
│   └── <version>-v<NNNN>.txt       # Data files, e.g., data/0.1.1-v0001.txt, data/0.1.2-v0001.txt
└── scripts/
    ├── tag_release.py              # Reads version.txt, creates git tag, bumps micro version (X.Y.Z -> X.Y.(Z+1))
    ├── simulate_commits.py         # Generates realistic multi-commit churn under data/ and tags releases
    ├── compact_by_tags.py          # Squashes all commits between consecutive tags into 1 commit per tag via git commit-tree
    └── verify_size_diff.py         # End-to-end benchmark: compares repo & fresh clone size before and after compaction
```

---

## 3. Core Specifications

### 3.1 `version.txt` & `data/RELEASE_MARKER.txt`

- `version.txt` sits at the repository root and contains a single semantic version string (starting at `0.1.1`).
- `data/RELEASE_MARKER.txt` is a hardcoded special sentinel file:
  - Whenever a release tag (e.g., `0.1.1`, `0.1.2`, `0.1.3`) is about to be created, `data/RELEASE_MARKER.txt` MUST be updated so its exact content equals that version string (e.g., `0.1.1\n`) and committed right before tagging.
  - This guarantees we can trivially verify that checking out any tag `X.Y.Z` restores `data/RELEASE_MARKER.txt` with content `X.Y.Z`.
- Version bumping rule: always increment the **micro (patch)** number by 1 (`0.1.1` -> `0.1.2` -> `0.1.3` -> ...).

### 3.2 `mise.toml` Tasks

Define the following tasks in `mise.toml`:

- `mise run tag`:
  Runs `python3 scripts/tag_release.py`:
  1. Reads the current version `V` from `version.txt` (e.g., `0.1.1`).
  2. Writes `V` into `data/RELEASE_MARKER.txt`.
  3. Commits `version.txt` and `data/RELEASE_MARKER.txt` if there are staged/unstaged changes (commit message: `"Release V"`).
  4. Creates a Git tag named exactly `V` (`git tag V`).
  5. Bumps `version.txt` to the next micro version (`0.1.2`) so subsequent work belongs to the next release cycle (do not tag the bumped version yet).
- `mise run simulate`:
  Runs `python3 scripts/simulate_commits.py` to generate multiple release cycles of heavy commit churn and push them to `origin`.
- `mise run compact`:
  Runs `python3 scripts/compact_by_tags.py` to compact the history in-place, run aggressive local GC, force-push to `origin`, and verify fresh clone size reduction.

### 3.3 `scripts/simulate_commits.py` (Realistic Text Churn & Bloat Generator)

This script simulates `R` release cycles (default `R = 5`, e.g., `0.1.1` through `0.1.5`) with `N` intermediate commits per cycle (default `N = 25`).

For each release cycle corresponding to target version `V` (read from `version.txt`, e.g., `0.1.2` after `0.1.1` was released):
1. **Macro-level file growth with versioned filenames**:
   - Create several new files during this cycle named strictly `data/{V}-v0001.txt`, `data/{V}-v0002.txt`, `data/{V}-v0003.txt`, etc. (incrementing `vNNNN` per new file in that version cycle).
   - So as versions progress (`0.1.1` -> `0.1.2` -> `0.1.3`), the total number of files in `data/` grows macroscopically.
2. **Line-level partial diffs (DO NOT overwrite entire files)**:
   - Each file in `data/{V}-vNNNN.txt` should be a structured multi-line plain-text document (e.g., 200 to 500 lines of simulated customer records, JSONL rows, or log blocks with high-entropy tokens like UUIDs/hashes/timestamps so Git cannot trivially deduplicate them).
   - In each intermediate commit, pick 1-2 newly created files from the current version and 2-3 existing older files from previous versions in `data/`.
   - **Modify specific line slices instead of replacing the whole file**: insert 50-150 new lines in the middle, delete 40-140 existing lines at specific line ranges, and mutate 20-30 lines in place. This produces realistic, readable `git diff` hunks (`+` and `-` lines) while forcing Git to store a new blob for every intermediate commit!
   - To maximize the contrast between `.git` history size and working-tree size (e.g., ~20-50 MB of `.git` history vs. ~1-2 MB of working tree files), have intermediate commits temporarily insert large blocks of high-entropy lines (~100-300 KB per commit) and trim most of them back down in subsequent commits before the tag is cut.
3. **Cut the tag**:
   - At the end of each cycle's `N` intermediate commits, invoke the tag release logic: write `V` into `data/RELEASE_MARKER.txt`, commit, create tag `V`, and bump `version.txt` to `micro + 1`.
4. **Print every Git command**:
   - Print progress clearly so the user sees commits, line diffs, and tags being created.

### 3.4 `scripts/compact_by_tags.py` (One-Click Tag-to-Tag Squash & Benchmark)

This is the core script. When executed, it MUST print every single `git` command it runs (`[CMD] git ...`) and its output so the user has 100% visibility into the plumbing operations.

Step-by-step workflow for `scripts/compact_by_tags.py`:

1. **Pre-Compaction Baseline**:
   - Measure and print:
     - Total commit count (`git rev-list --count HEAD`)
     - List of all tags sorted by creation time (`git tag --sort=creatordate`)
     - Working tree `data/` size vs. `.git/` directory size (`du -sh data .git`)
   - Record the original `tree` SHA (`git rev-parse <tag>^{tree}`) for every tag so we can cryptographically verify zero data loss after compaction.
2. **Plumbing Compaction via `git commit-tree`**:
   - Sort all tags strictly by creation time (`git tag --sort=creatordate`).
   - Iterate through the tags in order:
     - For the 1st tag (`t_0`), create a root commit (no `-p` parent) pointing directly to `t_0^{tree}`, preserving the original commit timestamp (`GIT_AUTHOR_DATE` / `GIT_COMMITTER_DATE`) and message `"Release <t_0>"`.
     - For each subsequent tag (`t_i`), create a single commit pointing to `t_i^{tree}` with `-p <new_commit_of_t_{i-1}>`, collapsing all intermediate commits between `t_{i-1}` and `t_i` into a single commit!
   - If `HEAD` on `main` has uncommitted/untagged commits after the last tag (`HEAD^{tree} != last_tag^{tree}`), append one final commit on top of the last tag's new commit pointing to `HEAD^{tree}` with message `"WIP: changes after latest tag"`.
3. **Update References**:
   - Repoint every tag to its new commit (`git tag -f <tag> <new_commit>`).
   - Reset `main` to the final new commit (`git checkout -B main <final_commit>`).
4. **Verify Cryptographic Integrity**:
   - Verify that for every tag, `git rev-parse <tag>^{tree}` is 100% identical to the pre-compaction tree SHA, and `data/RELEASE_MARKER.txt` at `<tag>` matches `<tag>`.
5. **Local Garbage Collection**:
   - Run and print:
     ```bash
     git reflog expire --expire=now --expire-unreachable=now --all
     git gc --prune=now --aggressive
     ```
   - Measure and print the new `.git/` directory size and new commit count (`git rev-list --count HEAD`).
6. **Push to Remote & Fresh Clone Size Comparison (if `origin` is configured)**:
   - If a remote `origin` exists (or when `--push` is passed):
     - Force push the compacted branch and tags:
       ```bash
       git push origin main --force
       git push origin --tags --force
       ```
     - Create a temporary directory (e.g., `/tmp/git-tag-squash-demo-fresh-clone`), run a fresh `git clone` from `origin`, and print a clean before-vs-after comparison table showing:
       - Commit count before vs. after (e.g., `130 commits -> 5 commits`)
       - `.git` size before compaction vs. fresh clone `.git` size after compaction (e.g., `~35 MB -> ~1 MB`).

---

## 4. Implementation Instructions for Claude Code

When I ask you to initialize and test this repo:
1. Create `version.txt` (initialized to `0.1.1`), `mise.toml`, and all scripts under `scripts/`.
2. Run the simulation to generate ~5 tags (`0.1.1` to `0.1.5`) with enough high-entropy intermediate line additions/deletions so that `.git` reaches at least **25 MB - 50 MB**, while the working tree `data/` stays around **1 MB - 2 MB**.
3. Push the bloated state to `origin` first (if remote is set), clone a baseline copy to measure the "Before" clone size, and then run `scripts/compact_by_tags.py` to demonstrate the full compaction, force-push, and fresh-clone size drop.