#!/usr/bin/env python3
"""awa: Agents Work Apart.

Workspaces for running several coding agents on one git repo:
  record  Watch a folder with inotify and commit every change to a hidden ref
          (refs/awa/record) without touching the branch, index or HEAD.
  new     Make an isolated copy of a repo (btrfs reflink, instant) on its own
          branch awa/<name>, at <repo>@<name>.
  merge   Merge a copy's branch and run a test in a temporary copy, then
          fast-forward the repo; bring the copy up to date.

Standard library only. Linux only (inotify via ctypes).
"""
import argparse
import ctypes
import ctypes.util
import errno
import hashlib
import os
import select
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

RECORD_REF = "refs/awa/record"
QUIET_SECONDS = 2.0     # commit after writes have been quiet this long
MAX_DELAY = 30.0        # but never wait longer than this during a burst
SAFETY_SECONDS = 300.0  # snapshot at least this often, in case events were lost
# Ignored dependency and build-cache folders that `awa rm` may delete without
# checking their contents: tools regenerate them.
REGENERABLE_DIRS = ("node_modules", ".venv", "venv", "__pycache__", "target",
                    ".next", ".turbo", ".cache", ".pytest_cache",
                    ".mypy_cache", "dist", "build")


def die(msg, code=1):
    print(f"awa: {msg}", file=sys.stderr)
    sys.exit(code)


def git(*args, cwd, env=None, check=True, input=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    r = subprocess.run(["git", *args], cwd=cwd, env=e, input=input,
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: "
                           f"{(r.stderr or '').strip()}")
    return r


def toplevel(path):
    r = git("rev-parse", "--show-toplevel", cwd=path, check=False)
    if r.returncode != 0:
        die(f"{path} is not inside a git repository")
    return Path(r.stdout.strip())


def git_dir(repo):
    return Path(git("rev-parse", "--absolute-git-dir", cwd=repo).stdout.strip())


# ---------------------------------------------------------------- recording

def snapshot(repo, reason="change"):
    """Commit the folder's current state to RECORD_REF if it changed.

    The private index starts each time as a copy of the real index, so the
    snapshot holds exactly what `git add -A` would stage now: tracked files
    (even ones matching an ignore rule) plus untracked files that aren't
    ignored. The agent's own index and HEAD are never touched. Returns the
    new commit id, or None when nothing changed.
    """
    gd = git_dir(repo)
    (gd / "awa").mkdir(exist_ok=True)
    priv = gd / "awa" / "index"
    real = gd / "index"
    if real.exists():
        tmp = priv.with_name("index.tmp")
        shutil.copyfile(real, tmp)  # git replaces index by rename: consistent
        os.replace(tmp, priv)
    elif priv.exists():
        priv.unlink()
    env = {"GIT_INDEX_FILE": str(priv)}
    git("add", "-A", "--", ".", cwd=repo, env=env)
    tree = git("write-tree", cwd=repo, env=env).stdout.strip()
    prev = git("rev-parse", "-q", "--verify", RECORD_REF + "^{commit}",
               cwd=repo, check=False).stdout.strip()
    if prev:
        prev_tree = git("rev-parse", prev + "^{tree}", cwd=repo).stdout.strip()
        if prev_tree == tree:
            return None
        changed = git("diff-tree", "-r", "--name-status", prev_tree, tree,
                      cwd=repo).stdout.strip()
    else:
        changed = "(first snapshot)"
    lines = changed.splitlines()
    if len(lines) > 50:
        lines = lines[:50] + [f"... and {len(lines) - 50} more"]
    head = git("rev-parse", "-q", "--verify", "HEAD", cwd=repo,
               check=False).stdout.strip()
    msg = (f"awa: {reason} {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n\n"
           + (f"HEAD {head}\n\n" if head else "") + "\n".join(lines) + "\n")
    who = {"GIT_AUTHOR_NAME": "awa-recorder", "GIT_AUTHOR_EMAIL": "awa@localhost",
           "GIT_COMMITTER_NAME": "awa-recorder",
           "GIT_COMMITTER_EMAIL": "awa@localhost"}
    args = ["commit-tree", tree] + (["-p", prev] if prev else [])
    commit = git(*args, cwd=repo, env=who, input=msg).stdout.strip()
    # Compare-and-swap so two recorders on one repo can't lose a snapshot.
    git("update-ref", "-m", "awa record", RECORD_REF, commit,
        prev or "0" * 40, cwd=repo)
    return commit


IN_MODIFY = 0x002
IN_ATTRIB = 0x004
IN_CLOSE_WRITE = 0x008
IN_MOVED_FROM = 0x040
IN_MOVED_TO = 0x080
IN_CREATE = 0x100
IN_DELETE = 0x200
IN_DELETE_SELF = 0x400
IN_MOVE_SELF = 0x800
IN_Q_OVERFLOW = 0x4000
IN_IGNORED = 0x8000
IN_ISDIR = 0x40000000
IN_ONLYDIR = 0x01000000
IN_NONBLOCK = 0x800
IN_CLOEXEC = 0x80000
WATCH_MASK = (IN_CLOSE_WRITE | IN_MODIFY | IN_ATTRIB | IN_MOVED_FROM
              | IN_MOVED_TO | IN_CREATE | IN_DELETE | IN_ONLYDIR)
EVENT_HDR = struct.Struct("iIII")


class Watcher:
    """Recursive inotify watch of a repo, skipping .git and ignored dirs."""

    def __init__(self, repo):
        self.repo = Path(repo)
        self.libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6",
                                use_errno=True)
        self.fd = self.libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self.wd_path = {}
        self.add_tree(str(self.repo))

    def ignored_dirs(self):
        """Directories git ignores (not just ones holding only ignored files)."""
        r = git("ls-files", "--others", "--ignored", "--exclude-standard",
                "--directory", "-z", cwd=self.repo, check=False)
        cand = [x.rstrip("/") for x in r.stdout.split("\0") if x.endswith("/")]
        if not cand:
            return set()
        r = git("check-ignore", "--stdin", "-z", cwd=self.repo, check=False,
                input="\0".join(cand) + "\0")
        return {str(self.repo / x) for x in r.stdout.split("\0") if x}

    def add_tree(self, top):
        skip = self.ignored_dirs()
        for root, subdirs, _ in os.walk(top):
            if root in skip:
                subdirs[:] = []
                continue
            subdirs[:] = [d for d in subdirs if d != ".git"
                          and os.path.join(root, d) not in skip
                          and not os.path.islink(os.path.join(root, d))]
            self.add(root)

    def add(self, path):
        wd = self.libc.inotify_add_watch(self.fd, os.fsencode(path), WATCH_MASK)
        if wd < 0:
            err = ctypes.get_errno()
            if err == errno.ENOSPC:
                raise OSError(err, "out of inotify watches; raise "
                              "fs.inotify.max_user_watches")
            return  # vanished before we could watch it; fine
        self.wd_path[wd] = path

    def drop_tree(self, top):
        for wd, p in list(self.wd_path.items()):
            if p == top or p.startswith(top + os.sep):
                self.libc.inotify_rm_watch(self.fd, wd)
                self.wd_path.pop(wd, None)

    def read(self):
        """Return True if anything in the repo changed."""
        changed = rescan = False
        try:
            data = os.read(self.fd, 1 << 16)
        except BlockingIOError:
            return False
        i = 0
        while i + EVENT_HDR.size <= len(data):
            wd, mask, _cookie, ln = EVENT_HDR.unpack_from(data, i)
            name = data[i + EVENT_HDR.size:i + EVENT_HDR.size + ln].rstrip(b"\0")
            i += EVENT_HDR.size + ln
            if mask & IN_Q_OVERFLOW:
                changed = rescan = True
                continue
            if mask & IN_IGNORED:
                self.wd_path.pop(wd, None)
                continue
            parent = self.wd_path.get(wd)
            if parent is None or not name:
                continue
            fname = os.fsdecode(name)
            if fname == ".git":
                continue
            changed = True
            path = os.path.join(parent, fname)
            if fname == ".gitignore":
                rescan = True  # newly ignored or unignored dirs
            if mask & IN_ISDIR:
                if mask & (IN_MOVED_FROM | IN_DELETE):
                    self.drop_tree(path)
                if mask & (IN_CREATE | IN_MOVED_TO):
                    self.add_tree(path)
        if rescan:
            self.rescan()
        return changed

    def rescan(self):
        for wd in list(self.wd_path):
            self.libc.inotify_rm_watch(self.fd, wd)
        self.wd_path.clear()
        self.add_tree(str(self.repo))


def record_loop(repo, once=False):
    repo = toplevel(repo)
    first = snapshot(repo, "start")
    print(f"awa: recording {repo} to {RECORD_REF}"
          + (f" (snapshot {first[:10]})" if first else ""), flush=True)
    if once:
        return
    w = Watcher(repo)
    pending_since = None
    last_event = 0.0
    last_snap = time.monotonic()
    while True:
        if not (repo / ".git").exists():
            print(f"awa: {repo} is gone; stopping", flush=True)
            return
        now = time.monotonic()
        if pending_since is not None:
            timeout = max(0.0, min(last_event + QUIET_SECONDS,
                                   pending_since + MAX_DELAY) - now)
        else:
            timeout = max(0.0, last_snap + SAFETY_SECONDS - now)
        ready, _, _ = select.select([w.fd], [], [], min(timeout, 10.0))
        now = time.monotonic()
        try:
            got = bool(ready) and w.read()
        except OSError:
            if not (repo / ".git").exists():
                continue  # the loop's next check stops cleanly
            raise
        if got:
            last_event = now
            if pending_since is None:
                pending_since = now
            if now - pending_since < MAX_DELAY:
                continue
        due = (pending_since is not None
               and (now - last_event >= QUIET_SECONDS
                    or now - pending_since >= MAX_DELAY))
        if due or now - last_snap >= SAFETY_SECONDS:
            try:
                c = snapshot(repo, "change" if due else "periodic")
                if c:
                    print(f"awa: snapshot {c[:10]}", flush=True)
            except (RuntimeError, OSError) as e:  # e.g. a lock; retry later
                print(f"awa: snapshot failed, will retry: {e}", flush=True)
                last_event = now
                continue
            pending_since = None
            last_snap = now


def unit_name(repo):
    h = hashlib.sha1(str(repo).encode()).hexdigest()[:10]
    return f"awa-record-{Path(repo).name[:40]}-{h}"


def record_start(repo):
    repo = toplevel(repo)
    unit = unit_name(repo)
    r = subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit])
    if r.returncode == 0:
        print(f"awa: already recording {repo} ({unit})")
        return
    subprocess.run(["systemctl", "--user", "reset-failed", unit],
                   capture_output=True)
    subprocess.run(["systemd-run", "--user", "--quiet", f"--unit={unit}",
                    "--property=Restart=on-failure",
                    "--property=RestartSec=5",
                    f"--working-directory={repo}",
                    sys.executable, os.path.abspath(__file__), "record",
                    str(repo)], check=True)
    print(f"awa: recording {repo} in the background ({unit}); "
          f"logs: journalctl --user -u {unit}")


def record_stop(repo):
    repo = toplevel(repo)
    unit = unit_name(repo)
    subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True)
    try:
        snapshot(repo, "stop")
    except RuntimeError:
        pass
    print(f"awa: stopped {unit}")


def is_recording(repo):
    return subprocess.run(["systemctl", "--user", "is-active", "--quiet",
                           unit_name(repo)]).returncode == 0


# ---------------------------------------------------------------- isolation

def copy_path(repo, name):
    return repo.parent / f"{repo.name}@{name}"


def check_name(name):
    if (not name or "/" in name or name.startswith(".") or " " in name
            or git("check-ref-format", "--branch", f"awa/{name}", cwd="/",
                   check=False).returncode != 0):
        die(f"bad workspace name {name!r}")


def dirty(repo, untracked=True):
    args = ["status", "--porcelain"] + ([] if untracked else ["-uno"])
    return git(*args, cwd=repo).stdout.strip()


def current_branch(repo):
    b = git("symbolic-ref", "-q", "--short", "HEAD", cwd=repo,
            check=False).stdout.strip()
    if not b:
        die(f"{repo} is on a detached HEAD; check out a branch first")
    return b


def reflink_copy(src, dest):
    r = subprocess.run(["cp", "-a", "--reflink=always", str(src), str(dest)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        print(f"awa: reflink copy failed ({r.stderr.strip()}); "
              "falling back to a full copy", file=sys.stderr)
        subprocess.run(["cp", "-a", str(src), str(dest)], check=True)


def ws_new(repo, name, record):
    repo = toplevel(repo)
    check_name(name)
    dest = copy_path(repo, name)
    if dest.exists():
        die(f"{dest} already exists")
    if (repo / ".git").is_file():
        die("repo is itself a git worktree; run awa from the main checkout")
    base = current_branch(repo)
    if dirty(repo, untracked=False):
        print("awa: note: the repo has uncommitted changes; the copy gets "
              "them too", file=sys.stderr)
    reflink_copy(repo, dest)
    # The copy must not share the repo's recorder state or refs.
    shutil.rmtree(git_dir(dest) / "awa", ignore_errors=True)
    for ref in git("for-each-ref", "--format=%(refname)", "refs/awa/",
                   cwd=dest).stdout.split():
        git("update-ref", "-d", ref, cwd=dest)
    git("switch", "-q", "-c", f"awa/{name}", cwd=dest)
    git("config", "awa.origin", str(repo), cwd=dest)
    git("config", "awa.name", name, cwd=dest)
    git("config", "awa.base", base, cwd=dest)
    print(f"awa: {dest} on branch awa/{name} (from {base})")
    if record:
        record_start(dest)


def resolve(repo, name):
    repo = toplevel(repo)
    check_name(name)
    dest = copy_path(repo, name)
    if not (dest / ".git").exists():
        die(f"no workspace {dest}")
    origin = git("config", "awa.origin", cwd=dest, check=False).stdout.strip()
    if Path(origin) != repo:
        die(f"{dest} is not a workspace of {repo}")
    return repo, dest


def ws_sync(repo, dest, quiet=False):
    """Bring the repo's current branch into the workspace's checked-out branch.

    The merge is computed with `git merge-tree`, which doesn't touch the
    working tree; the workspace only changes by a fast-forward, which git
    refuses if it would overwrite local or ignored files.
    """
    if dirty(dest, untracked=False):
        print(f"awa: {dest.name} has uncommitted changes; not syncing it. "
              f"Commit there, then run: awa sync {dest.name.split('@', 1)[1]}")
        return False
    base = current_branch(repo)
    git("fetch", "-q", str(repo), base, cwd=dest)
    theirs = git("rev-parse", "FETCH_HEAD", cwd=dest).stdout.strip()
    ours = git("rev-parse", "HEAD", cwd=dest).stdout.strip()
    if git("merge-base", "--is-ancestor", theirs, ours, cwd=dest,
           check=False).returncode == 0:
        target = None
    elif git("merge-base", "--is-ancestor", ours, theirs, cwd=dest,
             check=False).returncode == 0:
        target = theirs
    else:
        r = git("merge-tree", "--write-tree", "--name-only", ours, theirs,
                cwd=dest, check=False)
        if r.returncode == 1:
            files = r.stdout.split("\n\n", 1)[0].splitlines()[1:]
            print(f"awa: syncing {base} into {dest.name} conflicts in:\n"
                  + "".join(f"{f}\n" for f in files)
                  + "Left it unchanged; merge by hand there.")
            return False
        if r.returncode != 0:
            raise RuntimeError(f"git merge-tree failed in {dest}: "
                               f"{r.stderr.strip()}")
        tree = r.stdout.splitlines()[0]
        target = git("commit-tree", tree, "-p", ours, "-p", theirs, "-m",
                     f"Merge {base} into {dest.name}", cwd=dest).stdout.strip()
    if target:
        r = git("merge", "--ff-only", "-q", "--no-overwrite-ignore", target,
                cwd=dest, check=False)
        if r.returncode != 0:
            print(f"awa: syncing {base} into {dest.name} failed; left it "
                  f"unchanged:\n{(r.stderr or r.stdout).strip()}")
            return False
    if not quiet:
        print(f"awa: {dest.name} is up to date with {base}")
    return True


def try_merge(repo, dest, name, test):
    """Merge and test in a temporary copy; fast-forward the repo on success.

    The repo's working tree is only ever changed by a fast-forward, which git
    refuses if it would overwrite local or ignored files, so nothing in the
    repo can be lost. Returns 0 on success, 1 otherwise.
    """
    branch = f"awa/{name}"
    incoming = f"refs/awa/incoming/{name}"
    base = current_branch(repo)
    before = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    git("fetch", "-q", str(dest), f"+{branch}:{incoming}", cwd=repo)
    if git("merge-base", "--is-ancestor", incoming, "HEAD", cwd=repo,
           check=False).returncode == 0:
        print(f"awa: nothing new on {branch}")
        return 0
    cand = repo.parent / f".{repo.name}@{name}.merging"
    if cand.exists():
        print(f"awa: {cand} exists: another merge of {name} is running, or "
              "one crashed. Delete it if no merge is running.")
        return 1
    try:
        reflink_copy(repo, cand)
        git("reset", "-q", "--hard", "HEAD", cwd=cand)
        r = git("merge", "--no-ff", "--no-edit", "-q", "--no-overwrite-ignore",
                "-m", f"Merge workspace {name}", incoming, cwd=cand,
                check=False)
        if r.returncode != 0:
            files = git("diff", "--name-only", "--diff-filter=U",
                        cwd=cand).stdout
            print(f"awa: merging {branch} failed; {repo.name} is unchanged."
                  + (f" Conflicts in:\n{files}" if files else
                     f"\n{(r.stderr or r.stdout).strip()}\n"))
            return 1
        if test:
            t = subprocess.run(test, shell=True, cwd=cand)
            if t.returncode != 0:
                print(f"awa: test failed (exit {t.returncode}); "
                      f"{repo.name} is unchanged")
                return 1
        merged = git("rev-parse", "HEAD", cwd=cand).stdout.strip()
        git("fetch", "-q", str(cand), "HEAD", cwd=repo)
        now_branch = git("symbolic-ref", "-q", "--short", "HEAD", cwd=repo,
                         check=False).stdout.strip()
        now = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        if now_branch != base or now != before:
            print(f"awa: {repo.name} moved during the merge (was {base} at "
                  f"{before[:10]}); it is unchanged. Merge again.")
            return 1
        r = git("merge", "--ff-only", "-q", "--no-overwrite-ignore", merged,
                cwd=repo, check=False)
        if r.returncode != 0:
            print(f"awa: the merge passed but {repo.name} can't take it as a "
                  f"fast-forward; it is unchanged:\n"
                  f"{(r.stderr or r.stdout).strip()}\n"
                  "Commit or move the files named above, then merge again.")
            return 1
        print(f"awa: merged {branch} into {repo.name} ({merged[:10]})")
        return 0
    finally:
        shutil.rmtree(cand, ignore_errors=True)


def ws_merge(repo, name, test):
    repo, dest = resolve(repo, name)
    current_branch(repo)
    if git("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=repo,
           check=False).returncode == 0:
        die(f"{repo} is in the middle of a merge")
    if dirty(dest, untracked=False):
        print(f"awa: note: {dest.name} has uncommitted changes; only its "
              "commits are merged", file=sys.stderr)
    try:
        return try_merge(repo, dest, name, test)
    finally:
        try:
            ws_sync(repo, dest)
        except RuntimeError as e:
            print(f"awa: syncing {dest.name} failed: {e}", file=sys.stderr)


def unique_work(repo, dest):
    """Why deleting dest would lose work, or '' if it wouldn't."""
    if dirty(dest):
        return "it has uncommitted or untracked changes"
    if git("rev-parse", "-q", "--verify", "refs/stash", cwd=dest,
           check=False).returncode == 0:
        return "it has stashes"
    # Skip regenerable folders only where git ignores the folder itself.
    r = git("ls-files", "--others", "--ignored", "--exclude-standard",
            "--directory", "-z", cwd=dest)
    cand = [x.rstrip("/") for x in r.stdout.split("\0")
            if x.endswith("/") and os.path.basename(x.rstrip("/"))
            in REGENERABLE_DIRS]
    regen = []
    if cand:
        r = git("check-ignore", "--stdin", "-z", cwd=dest, check=False,
                input="\0".join(cand) + "\0")
        regen = [x for x in r.stdout.split("\0") if x]
    skip = [f":(exclude,literal){d}" for d in regen]
    r = git("ls-files", "--others", "--ignored", "--exclude-standard", "-z",
            "--", ".", *skip, cwd=dest)
    only = [x for x in r.stdout.split("\0")
            if x and not os.path.lexists(repo / x)]
    if only:
        more = f" and {len(only) - 5} more" if len(only) > 5 else ""
        return (f"it has ignored files {repo.name} doesn't: "
                f"{', '.join(only[:5])}{more}")
    git("fetch", "-q", "--prune", str(repo), "+refs/heads/*:refs/awa/origin/*",
        cwd=dest)
    n = git("rev-list", "--count", "HEAD", "--branches", "--tags", "--not",
            "--glob=refs/awa/origin/*", cwd=dest).stdout.strip()
    if n != "0":
        return f"{n} of its commits are on no branch of {repo.name}"
    return ""


def ws_rm(repo, name, force):
    repo, dest = resolve(repo, name)
    if not force:
        why = unique_work(repo, dest)
        if why:
            die(f"not removing {dest}: {why}. Merge first, or use --force")
    if is_recording(dest):
        record_stop(dest)
    # Keep the copy's edit history in the repo.
    if git("rev-parse", "-q", "--verify", RECORD_REF, cwd=dest,
           check=False).returncode == 0:
        kept = f"{RECORD_REF}-{name}-{time.strftime('%Y%m%dT%H%M%S')}"
        while git("rev-parse", "-q", "--verify", kept, cwd=repo,
                  check=False).returncode == 0:
            kept += "x"
        git("fetch", "-q", str(dest), f"{RECORD_REF}:{kept}", cwd=repo)
        print(f"awa: kept its edit history as {kept}")
    shutil.rmtree(dest)
    git("update-ref", "-d", f"refs/awa/incoming/{name}", cwd=repo, check=False)
    print(f"awa: removed {dest}")


def ws_list(repo):
    repo = toplevel(repo)
    base = current_branch(repo)
    found = False
    for p in sorted(repo.parent.glob(f"{repo.name}@*")):
        if not (p / ".git").exists():
            continue
        name = p.name.split("@", 1)[1]
        found = True
        git("fetch", "-q", str(repo), base, cwd=p, check=False)
        unmerged = git("rev-list", "--count", f"FETCH_HEAD..awa/{name}",
                       cwd=p, check=False).stdout.strip() or "?"
        flags = []
        if dirty(p, untracked=False):
            flags.append("uncommitted")
        if is_recording(p):
            flags.append("recording")
        print(f"{name}\t{p}\t{unmerged} unmerged commit(s)"
              + (f"\t{', '.join(flags)}" if flags else ""))
    if not found:
        print("awa: no workspaces")


# ---------------------------------------------------------------- cli

def main(argv=None):
    p = argparse.ArgumentParser(
        prog="awa", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-C", dest="repo", default=".",
                   help="repo to act on (default: current folder)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="record every edit to refs/awa/record")
    r.add_argument("path", nargs="?", default=None)
    g = r.add_mutually_exclusive_group()
    g.add_argument("--start", action="store_true", help="run in the background")
    g.add_argument("--stop", action="store_true", help="stop background recorder")
    g.add_argument("--once", action="store_true", help="take one snapshot")

    n = sub.add_parser("new", help="make an isolated copy <repo>@<name>")
    n.add_argument("name")
    n.add_argument("--record", action="store_true",
                   help="also start recording the copy")

    m = sub.add_parser("merge", help="merge a copy's branch into the repo")
    m.add_argument("name")
    m.add_argument("--test", help="command run in a temporary merged copy; "
                   "the repo is only updated if it passes")

    s = sub.add_parser("sync", help="bring the repo's branch into a copy")
    s.add_argument("name")

    sub.add_parser("list", help="list copies")

    d = sub.add_parser("rm", help="delete a copy whose work is all merged")
    d.add_argument("name")
    d.add_argument("--force", action="store_true")

    a = p.parse_args(argv)
    try:
        if a.cmd == "record":
            path = a.path or a.repo
            if a.start:
                record_start(path)
            elif a.stop:
                record_stop(path)
            else:
                record_loop(path, once=a.once)
        elif a.cmd == "new":
            ws_new(a.repo, a.name, a.record)
        elif a.cmd == "merge":
            return ws_merge(a.repo, a.name, a.test)
        elif a.cmd == "sync":
            repo, dest = resolve(a.repo, a.name)
            return 0 if ws_sync(repo, dest) else 1
        elif a.cmd == "list":
            ws_list(a.repo)
        elif a.cmd == "rm":
            ws_rm(a.repo, a.name, a.force)
    except RuntimeError as e:
        die(str(e))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
