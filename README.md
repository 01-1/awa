# awa: Agents Work Apart

*awa* (泡) is Japanese for "bubble": each agent works in its own.

A small command-line tool for running several coding agents on one git repo.
It does three things:

- **Records every edit.** `awa record` watches a folder with inotify. When
  writes have been quiet for 2 s (or after at most 30 s of constant writes),
  it commits the folder's state to `refs/awa/record` in the repo's history
  store (see below). It catches every write (shell edits, formatters,
  scripts), not only an agent's own edit tool. The repo's branch, index,
  HEAD, refs and objects are never touched. Each
  snapshot holds what `git add -A` would stage at that moment, so ignored
  files stay out. A missed event costs nothing: each snapshot commits whatever
  is on disk, and there is a safety snapshot every 5 minutes.
- **Isolates agents (opt-in, per task).** `awa new NAME` makes `<repo>@NAME`,
  a copy-on-write reflink copy on btrfs/XFS (a full copy elsewhere), on branch
  `awa/NAME`. It includes `node_modules` and build output, so there is nothing
  to reinstall.
- **Merges safely.** `awa merge NAME [--test CMD]` merges the copy's commits
  and runs the test in a temporary copy of the repo. The repo is only changed
  by a final fast-forward, which git refuses if it would overwrite local or
  ignored files, or if the repo moved meanwhile. A conflict or failed test
  leaves the repo as it was. Either way, the repo's branch is then merged
  back into the copy the same safe way (computed with `git merge-tree`,
  applied by fast-forward), so the agent sees everyone else's work.

## Use

```bash
awa record --start           # record this folder in the background (systemd user unit)
awa record --stop
awa log                      # the edit history (git log arguments pass through)
awa store                    # path of the history store
awa record --tree ~/code --start   # record every repo under ~/code from one process

awa new fix-auth --record    # isolated copy ../proj@fix-auth, recorded
awa merge fix-auth --test 'pytest -x tests/test_auth.py'
awa sync fix-auth            # merge the repo's branch into the copy
awa list
awa rm fix-auth              # refuses if anything there isn't on a repo branch
```

`-C PATH` acts on another repo. Only commits are merged: an agent's
uncommitted work stays in its copy (and in that copy's recorded history).
`awa rm` refuses to delete a copy with uncommitted files, stashes, commits
not on a branch of the repo, or ignored files the repo doesn't have (such
as a new `.env`), unless you pass `--force`. Ignored files that exist in both
are deleted without comparing contents, and so is anything inside ignored
dependency and build-cache folders (`node_modules`, `.venv`, `__pycache__`,
`target`, `.next`, `.turbo`, `.cache`, `dist`, `build` and the like), which
tools regenerate.
It keeps the copy's edit history in the repo's store as
`refs/awa/record-NAME-<timestamp>`, then deletes the copy's store.

## Where history is stored

Each repo's history is a bare git repo at
`~/.local/share/awa/<repo path below $HOME>.git` (`$XDG_DATA_HOME/awa/`),
for example `~/code/proj` → `~/.local/share/awa/code/proj.git`. Repos
outside `$HOME` go under `@abs/`. The store holds every object its history
needs, so it survives the repo being deleted or recloned. Inspect it with
`awa log`, or `git --git-dir "$(awa store)" show <commit>:<file>`.
History that older versions kept in the repo itself (`refs/awa/record*`) is
moved into the store on the next snapshot. A moved or renamed repo starts a
new store at its new path; the old one stays where it was.

## Recording many repos

`awa record --tree DIR` records every git repo up to 4 levels below `DIR`
from one process (about 20 MB, instead of one process per repo). It looks
for new and removed repos every minute. It skips git worktrees, folders
named like dependency or build caches (`node_modules`, `.venv` and the
like), repos that have their own recorder (`awa record --start`,
`awa new --record`), and repos with a file `.git/awa-norecord`.

## Ignoring folders: .awaignore

`.awaignore` in a repo, and `~/.config/awa/ignore` for every repo, hold
rules in `.gitignore` syntax that only awa uses. Matching folders aren't
watched, even when they hold tracked files: changes to those files are
still recorded, by the next snapshot of the repo (at most 5 minutes later).
Untracked files they match are left out of snapshots. Use them for large
regenerable or vendored folders that git tracks or doesn't ignore. The
repo's `.gitignore` files rank above these rules, so `!pattern` there
re-includes a path. git itself never reads them.

## When to isolate

Isolate when agents edit the same files or area, or when one agent's
half-finished work would break another's build or tests. Otherwise a shared
folder is cheaper. Isolated agents don't see each other's work until a merge,
so merge often. Copies need their own ports and databases if they run
servers.

## Install

```bash
ln -s "$PWD/awa.py" ~/.local/bin/awa
```

Python 3.9+, git 2.38+, Linux, systemd (only for `record --start`). No packages.
Tests: `python3 test_awa.py` (uses `~/.cache/awa-test`, or `AWA_TEST_DIR`).

## Limits

- The store grows without limit. It stores only changed files, but
  nothing prunes it.
- A git worktree as the source repo is refused. Run awa from the main
  checkout.
- The recorder uses one inotify watch per directory. Ignored directories
  (by `.gitignore` or `.awaignore`) aren't watched. fanotify can't replace
  this on a btrfs subvolume: there it reports writes but not creates,
  deletes or renames. Changes to `.git/info/exclude` aren't noticed until the next
  `.gitignore` change or restart.
- `--test` runs in a temporary copy, so tests that write to absolute paths
  in the repo affect the real repo.
