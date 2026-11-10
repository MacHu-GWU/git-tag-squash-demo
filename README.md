# git-tag-squash-demo

A testbed for a **tag-based Git history compaction** workflow: keep the exact
file tree of every release tag, throw away every intermediate commit in between,
and shrink `.git` (and every fresh clone) back down to roughly the size of the data.

## Background

You can treat a Git repository on GitHub as a "Google Drive for plain-text data":
a versioned warehouse of customer records, JSONL rows, logs and other text files
under `data/`. Git gives you diffs, history, access control and a free remote.

The catch is that day-to-day churn (lines added, deleted and edited, over and over)
creates hundreds of intermediate commits. Each one stores new blobs, so the
history grows to tens of MB while the files at any given release are only 1-2 MB.
For a data warehouse we do not care about those intermediate states. We only care
that **checking out release tag `X.Y.Z` returns exactly the files of that release**.

So the compaction keeps one commit per tag:

```text
before:  o-o-o-o-o-T1-o-o-o-o-o-o-T2-o-o-o-o-T3-o   (~140 commits, ~25-35 MB .git)
after:   T1' ---- T2' ---- T3' ---- WIP'             (1 commit per tag, ~1-2 MB .git)
```

Each new commit `Tn'` is created with `git commit-tree` and points at the **same
tree SHA** as the original tag, so the release contents are identical
byte for byte, and the scripts check this.

## Requirements

- [mise](https://mise.jdx.dev/) (installs Python 3.12 from `mise.toml`)
- Git, and optionally an `origin` remote on GitHub that you are happy to force-push to

Only the Python standard library is used.

## Layout

```text
version.txt                  # current version, e.g. "0.1.6" (the next release to cut)
data/RELEASE_MARKER.txt      # at tag X.Y.Z its content is exactly "X.Y.Z"
data/<version>-v<NNNN>.txt   # data files created during release cycle <version>
scripts/tag_release.py       # cut a release tag from version.txt, bump micro version
scripts/simulate_commits.py  # generate heavy line-level churn + tags, push to origin
scripts/compact_by_tags.py   # squash to 1 commit per tag, GC, force-push, fresh clone
scripts/verify_size_diff.py  # baseline clone -> compact -> fresh clone, size table
```

## Usage

Every step is a `mise` task. Extra flags are forwarded to the script.

### Step 1: generate a bloated history

```bash
mise run step01-simulate
```

This continues from the version in `version.txt`. With the defaults (5 releases x
25 commits) a run on a fresh repo starting at `0.1.1`:

- creates `0.1.1` ... `0.1.5`, 4 new `data/<version>-vNNNN.txt` files per release;
- edits files by line slices in every commit (insert 50-150 lines, delete 40-140,
  mutate 20-30 in place), plus a temporary 150-350 KB high-entropy block that
  later commits trim away again;
- before each tag: writes the version into `data/RELEASE_MARKER.txt`, commits
  `Release X.Y.Z`, tags it, then commits `Bump version to X.Y.(Z+1)`;
- pushes `main` and the tags to `origin`.

Useful flags: `--releases 3 --commits 10` (smaller run), `--seed 42`
(reproducible content), `--no-push`.

### Step 2: compact and benchmark

```bash
mise run step02-compact-and-benchmark
```

1. Clones the bloated state from `origin` into
   `/tmp/git-tag-squash-demo-baseline-clone` and measures it.
2. Runs `scripts/compact_by_tags.py`, which prints every git command it runs:
   baseline numbers, one `git commit-tree` per tag (plus a `WIP` commit if
   `main` moved past the latest tag), `git tag -f` / `git checkout -B main`,
   a check of every tag's tree SHA and `RELEASE_MARKER.txt`, then
   `git reflog expire` + `git gc --prune=now --aggressive`, then
   `git push --force` for `main` and the tags.
3. Clones the compacted state into `/tmp/git-tag-squash-demo-fresh-clone`, checks
   that every tag's tree SHA matches the baseline clone, and prints a table:

```text
+--------------------+----------+---------+------------+
| metric             | before   | after   | change     |
+--------------------+----------+---------+------------+
| commits on main    | 138      | 6       | -95.7%     |
| tags               | 5        | 5       | same trees |
| local .git         | 33.52 MB | 1.29 MB | -96.2%     |
| fresh clone .git   | 25.40 MB | 1.30 MB | -94.9%     |
| working tree data/ | 1.82 MB  | 1.82 MB | unchanged  |
+--------------------+----------+---------+------------+
```

If the remote is behind your local `main`, add `--push-baseline` to push the
bloated state first.

### Building blocks

| task | what it does |
| --- | --- |
| `mise run tag` | cut one release from `version.txt` (marker, commit, tag, bump) |
| `mise run compact` | compaction only (no baseline clone); `--no-push` keeps it local |
| `mise run simulate` / `mise run verify` | aliases of step 1 / step 2 |

## Running the demo again and again

The demo is meant to be **repeated**. Each run appends new tags on top of the
existing ones and never rewrites old releases:

```text
run 1: step01 -> 0.1.1 .. 0.1.5,  step02 -> 6 commits
run 2: step01 -> 0.1.6 .. 0.1.10, step02 -> 11 commits
run 3: step01 -> 0.1.11 .. 0.1.15 ...
```

Old files under `data/` stay, so the working tree grows a bit with every run
(about 1-2 MB per 5 releases). That growth is intentional: it is what a real
data warehouse looks like over time. Step 2 can run on a history that was
already compacted. Releases compacted earlier keep their commit, and only the
churn since then gets squashed.

Notes:

- Compaction **rewrites history** and force-pushes. Other clones of the repo
  must re-clone (or `git fetch && git reset --hard origin/main`).
- GitHub does not garbage-collect the old objects right away, but a fresh clone
  only downloads reachable objects, so the clone is small immediately.

## Resetting to a clean state

Everything generated lives under `data/`, in `version.txt` and in the tags, so a
full reset is simple:

1. Delete the GitHub repository and create it again, empty and with the same name.
2. Locally, keep only the toolkit and start from a fresh history:

   ```bash
   rm -rf data && printf '0.1.1\n' > version.txt
   ```

   ```bash
   rm -rf .git && git init -b main && git add -A && git commit -m "Initial commit"
   ```

   ```bash
   git remote add origin https://github.com/<owner>/git-tag-squash-demo.git && git push -u origin main
   ```

3. Run step 1 and step 2 again.
