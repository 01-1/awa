#!/usr/bin/env python3
"""awa: Agents Work Apart.

Workspaces for running several coding agents on one git repo:
  record  Watch a folder with inotify and commit every change to the ref
          refs/awa/record of a bare repo in ~/.local/share/awa/, without
          touching the folder's branch, index, HEAD, refs or objects.
          With --tree, one process records every repo under a folder.
  new     Make an isolated copy of a repo (btrfs reflink, instant) on its own
          branch awa/<name>, at <repo>@<name>.
  merge   Merge a copy's branch and run a test in a temporary copy, then
          fast-forward the repo; bring the copy up to date.
  log     Show a repo's recorded history (git log arguments pass through).

Standard library only. Linux only (inotify via ctypes).
"""
import argparse
import ctypes
import ctypes.util
import errno
import fcntl
import hashlib
import os
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RECORD_REF = "refs/awa/record"
QUIET_SECONDS = 2.0     # commit after writes have been quiet this long
MAX_DELAY = 30.0        # but never wait longer than this during a burst
SAFETY_SECONDS = 300.0  # snapshot at least this often, in case events were lost
REDISCOVER_SECONDS = 60.0  # --tree: look for new or removed repos this often
UPDATE_SECONDS = 86400.0  # auto-update: check for a new awa this often
TREE_DEPTH = 4          # --tree: how deep below the folder to look for repos
AWAIGNORE = ".awaignore"   # per-repo rules, gitignore syntax
NORECORD = "awa-norecord"  # <git dir>/awa-norecord: --tree skips the repo
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


# ---------------------------------------------------------------- ignore rules

def config_home():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def global_awaignore():
    return config_home() / "awa" / "ignore"


def excludes_file(repo, gd=None):
    """Write <git dir>/awa/excludes and return its path.

    It holds the user's global git excludes followed by awa's own rules
    (~/.config/awa/ignore, then the repo's .awaignore). Passed to git as
    core.excludesFile, so awa's rules use gitignore syntax and rank below the
    repo's .gitignore files, which can still re-include with `!`.
    """
    gd = gd or git_dir(repo)
    glob = git("config", "--path", "--get", "core.excludesFile", cwd=repo,
               check=False).stdout.strip()
    sources = [glob or config_home() / "git" / "ignore", global_awaignore(),
               Path(repo) / AWAIGNORE]
    parts = []
    for f in sources:
        try:
            parts.append(Path(f).read_text(errors="replace"))
        except OSError:
            parts.append("")
    (gd / "awa").mkdir(exist_ok=True)
    out = gd / "awa" / "excludes"
    _write_if_changed(out, "\n".join(parts) + "\n")
    # awa's rules alone, beside it: see awa_matches
    _write_if_changed(out.with_name("awa-rules"), "\n".join(parts[1:]) + "\n")
    return out


def _write_if_changed(path, text):
    try:
        if path.read_text(errors="replace") == text:
            return
    except OSError:
        pass
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def awa_matches(rels, excl):
    """Paths among rels (relative folders) that awa's own rules match,
    whatever the repo's .gitignore files say: checked in an empty scratch
    repo holding just those folders."""
    rules = Path(excl).with_name("awa-rules")
    try:
        if not rules.read_text(errors="replace").strip() or not rels:
            return set()
    except OSError:
        return set()
    with tempfile.TemporaryDirectory(prefix="awa-rules-") as t:
        git("init", "-q", cwd=t)
        for r in rels:
            os.makedirs(os.path.join(t, r), exist_ok=True)
        return check_ignore(t, list(rels), rules)


def check_ignore(repo, rels, excl=None):
    """Paths among rels (relative, existing) that ignore rules match.

    --no-index: a folder holding tracked files can still match. With excl,
    awa's rules count too.
    """
    if not rels:
        return set()
    pre = ("-c", f"core.excludesFile={excl}") if excl else ()
    r = git(*pre, "check-ignore", "--no-index", "--stdin", "-z", cwd=repo,
            check=False, input="\0".join(rels) + "\0")
    return {x for x in r.stdout.split("\0") if x}


def skipped_dirs(repo, dirs, excl):
    """Which of dirs (absolute paths in repo) the recorder doesn't watch.

    A folder is skipped when awa's rules match it (even if it holds tracked
    files: their changes are caught by the periodic snapshot), or when git
    ignores it and it holds no tracked files.
    """
    repo = str(repo)
    rel = {os.path.relpath(d, repo): d for d in dirs}
    skip = awa_matches(list(rel), excl)
    by_git = check_ignore(repo, [x for x in rel if x not in skip])
    if by_git:
        tracked = git("ls-files", "-c", "-z", "--", *sorted(by_git),
                      cwd=repo, check=False,
                      env={"GIT_LITERAL_PATHSPECS": "1"}).stdout.split("\0")
        holding = set()
        for f in tracked:
            parent = os.path.dirname(f)
            while parent:
                if parent in by_git:
                    holding.add(parent)
                parent = os.path.dirname(parent)
        skip |= by_git - holding
    return {rel[x] for x in skip if x in rel}


# ---------------------------------------------------------------- recording

def data_home():
    return Path(os.environ.get("XDG_DATA_HOME")
                or Path.home() / ".local" / "share")


def store_dir(repo):
    """The bare repo holding repo's edit history.

    ~/.local/share/awa/<repo path below $HOME>.git, or under @abs/ for a
    repo outside $HOME, so the history survives the repo being deleted.
    """
    repo = Path(os.path.realpath(repo))
    home = Path(os.path.realpath(Path.home()))
    if repo == home:
        rel = "@home"
    elif repo.is_relative_to(home):
        rel = str(repo.relative_to(home))
    else:
        rel = "@abs/" + str(repo.relative_to("/"))
    return data_home() / "awa" / (rel + ".git")


def open_store(repo):
    """Create repo's store if needed, move in any history still kept in the
    repo itself (refs/awa/record*, from older versions of awa), and return
    the store's path."""
    st = store_dir(repo)
    if not (st / "HEAD").exists():
        st.parent.mkdir(parents=True, exist_ok=True)
        tmp = st.with_name(f"{st.name}.tmp{os.getpid()}")
        shutil.rmtree(tmp, ignore_errors=True)
        git("init", "-q", "--bare", str(tmp), cwd="/")
        git("config", "awa.path", str(os.path.realpath(repo)), cwd=tmp)
        try:
            os.rename(tmp, st)
        except OSError:  # another recorder made it first
            shutil.rmtree(tmp, ignore_errors=True)
            if not (st / "HEAD").exists():
                raise
    old = git("for-each-ref", "--format=%(objectname) %(refname)",
              RECORD_REF, f"{RECORD_REF}-*", cwd=repo).stdout.split("\n")
    for line in filter(None, old):
        sha, ref = line.split(" ", 1)
        have = git("--git-dir", str(st), "rev-parse", "-q", "--verify", ref,
                   cwd="/", check=False).stdout.strip()
        dest = ref
        if have and have != sha:
            dest = f"{ref}-imported-{time.strftime('%Y%m%dT%H%M%S')}-{sha[:8]}"
        if have != sha:
            git("--git-dir", str(st), "fetch", "-q", str(repo),
                f"+{ref}:{dest}", cwd="/")
        git("update-ref", "-d", ref, sha, cwd=repo)
    return st


def copy_missing(repo, st, commit, prev, env):
    """Copy into the store the objects of commit (not in prev) that only the
    repo has, so the store never depends on the repo's objects."""
    objs = git("rev-list", "--objects", commit, *(["--not", prev] if prev
               else []), cwd=repo, env=env).stdout.split("\n")
    shas = "".join(o.split(" ", 1)[0] + "\n" for o in objs if o)
    r = git("--git-dir", str(st), "cat-file", "--batch-check", cwd="/",
            input=shas)
    missing = [x.split()[0] for x in r.stdout.splitlines()
               if x.endswith(" missing")]
    if not missing:
        return
    pack = subprocess.run(["git", "pack-objects", "--stdout", "-q"], cwd=repo,
                          input=("\n".join(missing) + "\n").encode(),
                          capture_output=True)
    if pack.returncode != 0:
        raise RuntimeError(f"git pack-objects failed in {repo}: "
                           f"{pack.stderr.decode(errors='replace').strip()}")
    cmd = ["unpack-objects", "-q"] if len(missing) < 1000 else [
        "index-pack", "--stdin"]
    r = subprocess.run(["git", "--git-dir", str(st), *cmd], input=pack.stdout,
                       capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {cmd[0]} failed in {st}: "
                           f"{r.stderr.decode(errors='replace').strip()}")


def snapshot(repo, reason="change"):
    """Commit the folder's current state to RECORD_REF in its store if it
    changed.

    The private index starts each time as a copy of the real index, so the
    snapshot holds exactly what `git add -A` would stage now: tracked files
    (even ones matching an ignore rule) plus untracked files that aren't
    ignored, where awa's own rules (.awaignore, ~/.config/awa/ignore) count
    as ignore rules. New objects go straight into the store; the agent's own
    index, HEAD, refs and objects are never touched.
    Returns the new commit id, or None when nothing changed.
    """
    gd = git_dir(repo)
    excl = excludes_file(repo, gd)
    st = open_store(repo)
    (gd / "awa").mkdir(exist_ok=True)
    priv = gd / "awa" / "index"
    real = gd / "index"
    if real.exists():
        tmp = priv.with_name("index.tmp")
        shutil.copyfile(real, tmp)  # git replaces index by rename: consistent
        os.replace(tmp, priv)
    elif priv.exists():
        priv.unlink()
    # Write to the store's objects; read the repo's too (the index names them).
    env = {"GIT_INDEX_FILE": str(priv),
           "GIT_OBJECT_DIRECTORY": str(st / "objects"),
           "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(gd / "objects")}
    git("-c", f"core.excludesFile={excl}", "add", "-A", "--", ".", cwd=repo,
        env=env)
    tree = git("write-tree", cwd=repo, env=env).stdout.strip()
    prev = git("--git-dir", str(st), "rev-parse", "-q", "--verify",
               RECORD_REF + "^{commit}", cwd="/", check=False).stdout.strip()
    if prev:
        prev_tree = git("--git-dir", str(st), "rev-parse", prev + "^{tree}",
                        cwd="/").stdout.strip()
        if prev_tree == tree:
            return None
        changed = git("diff-tree", "-r", "--name-status", prev_tree, tree,
                      cwd=repo, env=env).stdout.strip()
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
    commit = git(*args, cwd=repo, env={**env, **who}, input=msg).stdout.strip()
    copy_missing(repo, st, commit, prev, env)
    # Compare-and-swap so two recorders on one repo can't lose a snapshot.
    git("--git-dir", str(st), "update-ref", "-m", "awa record", RECORD_REF,
        commit, prev or "0" * 40, cwd="/")
    git("--git-dir", str(st), "gc", "--auto", "--quiet", cwd="/", check=False)
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
        try:
            self.excl = excludes_file(self.repo)
            self.add_tree(str(self.repo))
        except BaseException:
            os.close(self.fd)
            raise

    def close(self):
        os.close(self.fd)

    def add_tree(self, top):
        """Watch top and the folders below it, level by level, so each level
        costs a few git calls and skipped folders are never entered."""
        if top != str(self.repo) and skipped_dirs(self.repo, [top], self.excl):
            return
        level = [top]
        while level:
            below = []
            for d in level:
                self.add(d)
                try:
                    with os.scandir(d) as it:
                        below += [e.path for e in it if e.name != ".git"
                                  and e.is_dir(follow_symlinks=False)]
                except OSError:
                    pass  # vanished or unreadable; fine
            skip = skipped_dirs(self.repo, below, self.excl) if below else ()
            level = [d for d in below if d not in skip]

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
            if fname in (".gitignore", AWAIGNORE):
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
        self.excl = excludes_file(self.repo)
        self.add_tree(str(self.repo))


class Recorder:
    """One repo's watcher and snapshot timing."""

    def __init__(self, repo, offset=0.0, label=""):
        self.repo = Path(repo)
        self.label = label  # message prefix; names the repo under --tree
        self.watcher = Watcher(self.repo)
        self.pending_since = None
        self.last_event = 0.0
        self.last_snap = time.monotonic() - offset

    def gone(self):
        return not (self.repo / ".git").exists()

    def deadline(self):
        if self.pending_since is not None:
            return min(self.last_event + QUIET_SECONDS,
                       self.pending_since + MAX_DELAY)
        return self.last_snap + SAFETY_SECONDS

    def on_ready(self, now):
        try:
            got = self.watcher.read()
        except OSError:
            if self.gone():
                return  # the caller's next check drops it cleanly
            raise
        if got:
            self.last_event = now
            if self.pending_since is None:
                self.pending_since = now

    def maybe_snapshot(self, now):
        pending = self.pending_since is not None
        due = pending and (now - self.last_event >= QUIET_SECONDS
                           or now - self.pending_since >= MAX_DELAY)
        if not (due or now - self.last_snap >= SAFETY_SECONDS):
            return
        try:
            c = snapshot(self.repo, "change" if due else "periodic")
            if c:
                print(f"awa: {self.label}snapshot {c[:10]}", flush=True)
        except (RuntimeError, OSError) as e:  # e.g. a lock; retry later
            print(f"awa: {self.label}snapshot failed, will retry: {e}",
                  flush=True)
            self.last_event = now  # retry after QUIET_SECONDS, not at once
            if self.pending_since is None:
                self.pending_since = now
            return
        self.pending_since = None
        self.last_snap = now


def record_loop(repo, once=False):
    repo = toplevel(repo)
    first = snapshot(repo, "start")
    print(f"awa: recording {repo} to {RECORD_REF} in {store_dir(repo)}"
          + (f" (snapshot {first[:10]})" if first else ""), flush=True)
    if once:
        return
    r = Recorder(repo)
    rules = rules_stamp()
    while True:
        maybe_reexec()
        if r.gone():
            print(f"awa: {repo} is gone; stopping", flush=True)
            return
        if rules_stamp() != rules:  # ~/.config/awa/ignore changed
            rules = rules_stamp()
            r.watcher.rescan()
        timeout = max(0.0, r.deadline() - time.monotonic())
        ready, _, _ = select.select([r.watcher.fd], [], [], min(timeout, 10.0))
        now = time.monotonic()
        if ready:
            r.on_ready(now)
        r.maybe_snapshot(now)


def rules_stamp():
    """Changes when ~/.config/awa/ignore does."""
    try:
        st = os.stat(global_awaignore())
        return (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        return None


def discover(root, depth=TREE_DEPTH):
    """Git repos (main checkouts, not worktrees) at most depth levels below
    root, skipping dependency folders and repos with <git dir>/awa-norecord."""
    root = os.path.realpath(root)
    base = root.rstrip(os.sep).count(os.sep)
    found = []
    for d, subdirs, _ in os.walk(root):
        if ".git" in subdirs and not os.path.exists(
                os.path.join(d, ".git", NORECORD)):
            found.append(d)
        if d.count(os.sep) - base >= depth:
            subdirs[:] = []
        else:
            subdirs[:] = [s for s in subdirs if s != ".git"
                          and s not in REGENERABLE_DIRS]
    return found


def recording_units():
    """Names of the active per-repo recorder units (awa record --start)."""
    try:
        r = subprocess.run(["systemctl", "--user", "list-units", "--plain",
                            "--no-legend", "--state=active", "awa-record-*"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return set()
    return {line.split()[0].removesuffix(".service")
            for line in r.stdout.splitlines() if line.strip()}


def record_tree(root, once=False, stop=None):
    """Record every repo under root from one process.

    Finds repos again every REDISCOVER_SECONDS, so new ones are picked up and
    removed ones dropped. A repo that already has its own recorder unit
    (awa record --start, awa new --record) is left to it.
    """
    root = os.path.realpath(root)
    if not os.path.isdir(root):
        die(f"{root} is not a folder")
    recs = {}
    rules = rules_stamp()
    next_scan = 0.0
    while stop is None or not stop.is_set():
        now = time.monotonic()
        if now >= next_scan:
            if not once:
                maybe_reexec()
            next_scan = now + REDISCOVER_SECONDS
            busy = recording_units()
            want = [d for d in discover(root) if unit_name(Path(d)) not in busy]
            before = len(recs)
            for d in [d for d in recs if d not in want]:
                recs.pop(d).watcher.close()
                print(f"awa: {d}: no longer recorded here", flush=True)
            for d in want:
                if d in recs:
                    continue
                try:
                    first = snapshot(d, "start")
                    if once:
                        if first:
                            print(f"awa: {d}: snapshot {first[:10]}",
                                  flush=True)
                        continue
                    h = int(hashlib.sha1(d.encode()).hexdigest()[:8], 16)
                    recs[d] = Recorder(d, label=f"{d}: ",
                                       offset=SAFETY_SECONDS * (h % 1000) / 1000)
                except (RuntimeError, OSError) as e:
                    print(f"awa: {d}: not recording: {e}", flush=True)
            if once:
                return
            if len(recs) != before:
                print(f"awa: recording {len(recs)} repos under {root}",
                      flush=True)
            if rules_stamp() != rules:  # ~/.config/awa/ignore changed
                rules = rules_stamp()
                for d, r in list(recs.items()):
                    try:
                        r.watcher.rescan()
                    except (RuntimeError, OSError) as e:
                        print(f"awa: {d}: rescan failed: {e}", flush=True)
        fds = {r.watcher.fd: r for r in recs.values()}
        deadline = min([r.deadline() for r in recs.values()] + [next_scan])
        timeout = min(max(0.0, deadline - time.monotonic()), 10.0)
        ready, _, _ = select.select(list(fds), [], [], timeout)
        now = time.monotonic()
        for fd in ready:
            r = fds[fd]
            try:
                r.on_ready(now)
            except (RuntimeError, OSError) as e:
                print(f"awa: {r.label}watch failed, retrying later: {e}",
                      flush=True)
                recs.pop(str(r.repo), None)
                r.watcher.close()
        for d, r in list(recs.items()):
            if r.gone():
                recs.pop(d).watcher.close()
                print(f"awa: {d} is gone", flush=True)
                continue
            r.maybe_snapshot(now)
    for r in recs.values():
        r.watcher.close()


def unit_name(repo):
    h = hashlib.sha1(str(repo).encode()).hexdigest()[:10]
    return f"awa-record-{Path(repo).name[:40]}-{h}"


def tree_unit_name(root):
    h = hashlib.sha1(str(root).encode()).hexdigest()[:10]
    return f"awa-tree-{Path(root).name[:40]}-{h}"


def record_start(path, tree=False):
    if tree:
        target = Path(os.path.realpath(path))
        unit, args = tree_unit_name(target), ["record", "--tree", str(target)]
    else:
        target = toplevel(path)
        unit, args = unit_name(target), ["record", str(target)]
    r = subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit])
    if r.returncode == 0:
        print(f"awa: already recording {target} ({unit})")
        return
    subprocess.run(["systemctl", "--user", "reset-failed", unit],
                   capture_output=True)
    subprocess.run(["systemd-run", "--user", "--quiet", f"--unit={unit}",
                    "--property=Restart=on-failure",
                    "--property=RestartSec=5",
                    f"--working-directory={target}",
                    sys.executable, os.path.abspath(__file__), *args],
                   check=True)
    print(f"awa: recording {target} in the background ({unit}); "
          f"logs: journalctl --user -u {unit}")


def record_stop(path, tree=False):
    if tree:
        unit = tree_unit_name(Path(os.path.realpath(path)))
        subprocess.run(["systemctl", "--user", "stop", unit],
                       capture_output=True)
        print(f"awa: stopped {unit}")
        return
    repo = toplevel(path)
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
    # Keep the copy's edit history in the repo's store.
    src = open_store(dest)
    st = open_store(repo)
    if git("--git-dir", str(src), "rev-parse", "-q", "--verify", RECORD_REF,
           cwd="/", check=False).returncode == 0:
        kept = f"{RECORD_REF}-{name}-{time.strftime('%Y%m%dT%H%M%S')}"
        while git("--git-dir", str(st), "rev-parse", "-q", "--verify", kept,
                  cwd="/", check=False).returncode == 0:
            kept += "x"
        git("--git-dir", str(st), "fetch", "-q", str(src),
            f"{RECORD_REF}:{kept}", cwd="/")
        print(f"awa: kept its edit history as {kept} in {st}")
    shutil.rmtree(dest)
    shutil.rmtree(src, ignore_errors=True)
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


# ---------------------------------------------------------------- updates

def awa_checkout():
    """The git checkout awa.py runs from (through any symlink)."""
    return Path(os.path.realpath(__file__)).parent


def auto_update_enabled():
    """On unless AWA_NO_AUTO_UPDATE is set or ~/.config/awa/no-auto-update
    exists."""
    return not (os.environ.get("AWA_NO_AUTO_UPDATE")
                or (config_home() / "awa" / "no-auto-update").exists())


def update_stamp():
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return cache / "awa" / "last-update-check"


def update_due():
    """Whether auto-update should check now; at most once per UPDATE_SECONDS
    across all awa processes."""
    if not auto_update_enabled():
        return False
    try:
        return time.time() - update_stamp().stat().st_mtime >= UPDATE_SECONDS
    except OSError:
        return True


def self_update(src=None, quiet=False, auto=False):
    """Fast-forward awa's checkout to its upstream branch.

    Leaves it alone when tracked files have local changes, when it isn't on
    a branch with an upstream, or when it has commits the upstream lacks.
    One update runs at a time; with auto, only when one is due.
    Returns True when the checkout moved to a new commit.
    """
    src = Path(src or awa_checkout())
    say = (lambda m: None) if quiet else (lambda m: print(f"awa: {m}",
                                                          flush=True))
    p = update_stamp()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        lock = open(p.with_name("update.lock"), "w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        say("not updating: another update is running")
        return False
    with lock:
        if auto and not update_due():
            return False
        try:
            p.touch()
        except OSError:
            pass
        return _ff_checkout(src, say)


def _ff_checkout(src, say):
    env = {"GIT_TERMINAL_PROMPT": "0"}
    if "GIT_SSH_COMMAND" not in os.environ:
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"

    def run(*args, timeout=30):
        return subprocess.run(["git", *args], cwd=src, capture_output=True,
                              text=True, timeout=timeout,
                              env={**os.environ, **env})
    try:
        if run("rev-parse", "--is-inside-work-tree").returncode != 0:
            say(f"not updating: {src} is not a git checkout")
            return False
        if run("rev-parse", "--abbrev-ref", "@{u}").returncode != 0:
            say(f"not updating: {src} has no upstream branch")
            return False
        if run("status", "--porcelain", "--untracked-files=no").stdout.strip():
            say(f"not updating: {src} has local changes")
            return False
        old = run("rev-parse", "HEAD").stdout.strip()
        f = run("fetch", "--quiet", timeout=120)
        if f.returncode != 0:
            say(f"update check failed: {f.stderr.strip()}")
            return False
        m = run("merge", "--ff-only", "--quiet", "@{u}")
        if m.returncode != 0:
            say(f"not updating: {src} has commits its upstream lacks")
            return False
        new = run("rev-parse", "HEAD").stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        say(f"update check failed: {e}")
        return False
    if new == old:
        say("awa is up to date")
        return False
    print(f"awa: updated {old[:10]} -> {new[:10]}", flush=True)
    return True


def checkout_head():
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=awa_checkout(),
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


_running = {"head": None, "next": 0.0}  # this recorder's code and next check


def maybe_reexec():
    """In a long-running recorder, once a day: auto-update if due, and
    restart on the new code if the checkout moved to another commit (by
    this or any other awa process) and it compiles. A restart loses
    nothing: the recorder snapshots every repo when it starts."""
    now = time.monotonic()
    if _running["head"] is None:
        _running["head"] = checkout_head()
    if now < _running["next"] or not auto_update_enabled():
        return
    _running["next"] = now + UPDATE_SECONDS
    self_update(quiet=True, auto=True)
    head = checkout_head()
    if head is None or head == _running["head"]:
        return
    me = os.path.realpath(__file__)
    r = subprocess.run([sys.executable, "-m", "py_compile", me],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"awa: new version does not compile; still running the old "
              f"one: {r.stderr.strip()}", flush=True)
        return
    print("awa: restarting on the new version", flush=True)
    os.execv(sys.executable, [sys.executable, me, *sys.argv[1:]])


def background_update():
    """From a short command: check for an update in a detached process, so
    the command never waits on the network."""
    if not update_due():
        return
    try:
        subprocess.Popen([sys.executable, os.path.realpath(__file__),
                          "update", "--quiet", "--auto"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


# ---------------------------------------------------------------- cli

def main(argv=None):
    p = argparse.ArgumentParser(
        prog="awa", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-C", dest="repo", default=".",
                   help="repo to act on (default: current folder)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="record every edit to refs/awa/record "
                       "in ~/.local/share/awa/<repo>.git")
    r.add_argument("path", nargs="?", default=None)
    g = r.add_mutually_exclusive_group()
    g.add_argument("--start", action="store_true", help="run in the background")
    g.add_argument("--stop", action="store_true", help="stop background recorder")
    g.add_argument("--once", action="store_true", help="take one snapshot")
    r.add_argument("--tree", action="store_true",
                   help="record every git repo under path, from one process")

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

    sub.add_parser("log", help="show the recorded edit history; other "
                   "arguments go to git log (default ref: refs/awa/record)")

    sub.add_parser("store", help="print the path of the repo's history store")

    d = sub.add_parser("rm", help="delete a copy whose work is all merged")
    d.add_argument("name")
    d.add_argument("--force", action="store_true")

    u = sub.add_parser("update", help="update awa now from its git upstream")
    u.add_argument("--quiet", action="store_true")
    u.add_argument("--auto", action="store_true", help=argparse.SUPPRESS)

    a, extra = p.parse_known_args(argv)
    if extra and a.cmd != "log":
        p.error(f"unrecognized arguments: {' '.join(extra)}")
    if a.cmd not in ("record", "update"):
        background_update()
    try:
        if a.cmd == "update":
            self_update(quiet=a.quiet, auto=a.auto)
        elif a.cmd == "record":
            path = a.path or a.repo
            if a.start:
                record_start(path, tree=a.tree)
            elif a.stop:
                record_stop(path, tree=a.tree)
            elif a.tree:
                record_tree(path, once=a.once)
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
        elif a.cmd == "store":
            print(open_store(toplevel(a.repo)))
        elif a.cmd == "log":
            st = open_store(toplevel(a.repo))
            return subprocess.run(["git", "--git-dir", str(st), "log",
                                   *extra, *([] if any(not x.startswith("-")
                                   for x in extra) else [RECORD_REF])
                                   ]).returncode
    except RuntimeError as e:
        die(str(e))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
