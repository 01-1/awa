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
- **Isolates agents (opt-in, per task).** `awa new NAME` makes `<repo>@NAME`
  on branch `awa/NAME`. If the repo folder is a btrfs subvolume, the copy is
  a snapshot of it; otherwise it is a copy-on-write reflink copy on btrfs/XFS
  (a full copy elsewhere). It includes `node_modules` and build output, so
  there is nothing to reinstall.
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
awa log --stat                 # the edit history (git log refs/awa/record ARGS)
awa store                    # path of the history store
awa record --tree ~/code --start   # record every repo under ~/code from one process

awa new fix-auth --record    # isolated copy ../proj@fix-auth, recorded
awa merge fix-auth --test 'pytest -x tests/test_auth.py'
awa sync fix-auth            # merge the repo's branch into the copy
awa list
awa rm fix-auth              # refuses if anything there isn't on a repo branch
awa subvol                   # make this repo folder a btrfs subvolume (once)
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

## Snapshots on btrfs

On btrfs, make the repo folder a subvolume with `awa subvol`; `awa new` then
copies it with `btrfs subvolume snapshot`. A snapshot shares the
filesystem's metadata with its source: it takes constant time, and only the
files that change afterwards cost anything. A reflink copy instead creates a
new inode and extent reference for every file, which for a repo with a large
`node_modules` (hundreds of thousands of files) bloats btrfs metadata and
slows every filesystem commit. Subvolumes nested in another subvolume are
also left out of snapshots of the outer one (such as snapper's snapshots of
`/home`), so agents' edits don't pile up there. Merges (`awa merge`) make
their temporary copy the same way.

`awa subvol` creates a subvolume beside the folder, copies the folder into
it with reflinks, checks that every path, type, mode, size, modification
time and link target matches, swaps the two names, and deletes the old
folder. It refuses on other filesystems, and while any process has its
current folder, open files or inotify watches inside the repo (shown with
their process ids), including an `awa record` recorder: stop it, convert,
and start it again. Files that were hard links to files outside the repo
(pnpm's store, for example) become reflink copies.

`awa rm` deletes a snapshot copy with `btrfs subvolume delete`. Without root,
that needs the `user_subvol_rm_allowed` mount option; without it, awa
deletes the files one by one and then the empty subvolume, which anyone who
owns it may do. A snapshot holds only the repo's own subvolume: subvolumes
nested inside the repo appear in the copy as empty folders.

## Where history is stored

Each repo's history is a bare git repo at
`~/.local/share/awa/<repo path below $HOME>.git` (`$XDG_DATA_HOME/awa/`),
for example `~/code/proj` → `~/.local/share/awa/code/proj.git`. Repos
outside `$HOME` go under `@abs/`. The store holds every object its history
needs, so it survives the repo being deleted or recloned. Inspect it with
`awa log`, or `git --git-dir "$(awa store)" show <commit>:<file>`.
History that older versions kept in the repo itself (`refs/awa/record*`) is
moved into the store on the next snapshot.

A moved or renamed repo keeps its history. awa writes a random id to
`.git/awa/id` and keeps it in the store as `awa.id`. When a repo shows up at
a new path, awa looks for a store whose repo is gone from its old path and
has the same id, and renames it to the new path (or, if a store is already
there, copies its refs in beside the existing ones as
`refs/awa/record-moved-<time>-<commit>` and deletes it). Stores of repos
nested inside the moved one follow it. This works whether the move happened
while awa was running or not. Stores made before awa kept ids match when the
repo's HEAD reflog, and no other recorded repo's, holds the commit their last
snapshot was taken at. A copy
of a recorded repo, with the original still present, gets an id and a store
of its own. If a new repo takes a moved repo's old path first, the old store
is renamed aside (`<name>@awa-moved-<id>-<time>.git`) until the moved repo
claims it.

## Recording many repos

`awa record --tree DIR` records every git repo up to 4 levels below `DIR`
from one process (about 20 MB, instead of one process per repo). It looks
for new and removed repos every minute. It skips git worktrees, folders
named like dependency or build caches (`node_modules`, `.venv` and the
like), repos that have their own recorder (`awa record --start`,
`awa new --record`), and repos with a file `.git/awa-norecord`.

## Backups

`awa backup` backs up every history store (all of `~/.local/share/awa`)
off the machine with [restic](https://restic.net). Settings live in
`~/.config/awa/backup.env`, one `KEY=VALUE` per line (`export ` and quotes
allowed; comments only on their own `#` lines). Secrets stay in files it
points to, and none go on a command line:

```bash
# any restic repository
RESTIC_REPOSITORY=b2:my-bucket:awa
RESTIC_PASSWORD_FILE=~/credentials/restic/password
# optional: a KEY=VALUE file of provider keys, such as B2_ACCOUNT_ID
AWA_BACKUP_ENV_FILE=~/credentials/backblaze/b2.env
# optional: snapshot host (default: the hostname), forget policy for --prune
#AWA_BACKUP_HOST=laptop
#AWA_BACKUP_KEEP=--keep-daily 30 --keep-weekly 26
```

```bash
awa backup --init       # create the restic repository if it doesn't exist
awa backup              # back up now
awa backup --start      # hourly backup, weekly prune (systemd user timers)
awa backup --stop
awa backup --prune      # forget old backups, then prune
awa backup --restic snapshots   # any restic command with these settings
```

Snapshots carry the tag `awa` and the host, and `--prune` only forgets
snapshots with both. It keeps 48 hourly, 30 daily, 26 weekly and 24
monthly snapshots. It never repacks (`--max-unused unlimited
--repack-cacheable-only --max-repack-size 0`): it deletes only packs that
nothing uses, so it downloads no pack data, which matters where downloads are capped (B2).
Backups and prunes take one lock, so they never overlap: a backup that
finds the lock taken skips that hour, and a prune waits.

**Consistency.** Stores keep changing while recorders run: a snapshot
writes its objects and then moves its ref, and `git gc` writes a new pack
and then deletes what it replaced, and packs loose refs, then deletes
them. A plain file-by-file backup can catch a ref whose objects it
missed, or miss a ref. So `awa backup` first copies each store to
`~/.local/state/awa/backup-stage` (reflinks on btrfs/XFS: no extra space;
about 5 s for 4 GB of stores). A copy is kept only if its refs equal the
store's just before and just after copying, and
`git fsck --connectivity-only` finds every object they need; otherwise
the store is copied again. restic then reads the checked copies, and they
are deleted. A store with no such copy after 5 tries (broken at the
source, or rewritten nonstop) is backed up as copied and `awa backup`
exits 1, naming it; earlier snapshots keep its last good version.

Snapshots hold the stores under the staging path. To restore them:

```bash
awa backup --restic restore "latest:$HOME/.local/state/awa/backup-stage" \
  --tag awa --host "$(hostname)" --target /tmp/awa-restore
```

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

awa updates itself: once a day, a running recorder or any other awa
command fast-forwards awa's own git checkout to its upstream branch
(a recorder then restarts on the new code). It skips the update when the
checkout has local changes or commits the upstream lacks. Turn this off
with `touch ~/.config/awa/no-auto-update` (or `AWA_NO_AUTO_UPDATE=1`);
`awa update` updates now either way.

Python 3.9+, git 2.38+, Linux, systemd (only for `record --start` and
`backup --start`), restic (only for `backup`), btrfs-progs (only for
subvolume snapshots). No packages.
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
