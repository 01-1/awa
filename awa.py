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
  backup  Back up every history store with restic, from a checked copy;
          --prune forgets old backups without downloading pack data.

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
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid
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


def stores_root():
    return data_home() / "awa"


def read_id(gd):
    """The random id in <git dir>/awa/id that ties a repo to its store
    wherever the repo moves; None if it has none."""
    try:
        return (Path(gd) / "awa" / "id").read_text().strip() or None
    except OSError:
        return None


def new_id(gd):
    (Path(gd) / "awa").mkdir(exist_ok=True)
    rid = uuid.uuid4().hex
    _write_if_changed(Path(gd) / "awa" / "id", rid + "\n")
    return rid


def path_id(path):
    """The id of the repo at path: '' if it has none, None if no repo is
    there."""
    if not path:
        return None
    dot = Path(path) / ".git"
    if dot.is_dir():
        return read_id(dot) or ""
    if dot.is_file():  # a worktree or submodule: ask git where its dir is
        r = git("rev-parse", "--absolute-git-dir", cwd=path, check=False)
        if r.returncode == 0:
            return read_id(r.stdout.strip()) or ""
    return None


INDEX_SECONDS = 60.0  # how long a scan of the stores is reused
_index = {}    # stores root -> (time, {store: {"path", "id", ...}})
_checked = {}  # (stores root, store) -> id of the repo this process checked


def _store_info(st):
    r = git("config", "-f", str(st / "config"), "--get-regexp", r"^awa\.",
            cwd="/", check=False)
    info = {}
    for line in r.stdout.splitlines():
        k, _, v = line.partition(" ")
        info[k[len("awa."):]] = v
    return info


def store_index():
    """Every store under stores_root(), with its awa.path and awa.id."""
    root = stores_root()
    hit = _index.get(str(root))
    if hit and time.monotonic() - hit[0] < INDEX_SECONDS:
        return hit[1]
    found = {}
    for d, subdirs, _ in os.walk(root):
        keep = []
        for s in subdirs:
            p = os.path.join(d, s)
            if s.endswith(".git") and os.path.exists(os.path.join(p, "HEAD")):
                found[p] = _store_info(Path(p))
            elif ".git.tmp" not in s:
                keep.append(s)
        subdirs[:] = keep
    _index[str(root)] = (time.monotonic(), found)
    return found


def _live(info):
    """Whether the repo a store was made for is still at its path."""
    pid = path_id(info.get("path"))
    return pid is not None and (not info.get("id") or pid == info["id"])


def _last_head(st, info):
    """The repo HEAD named by the store's newest snapshot (cached)."""
    if "head" not in info:
        msg = git("--git-dir", str(st), "log", "-1", "--format=%B",
                  RECORD_REF, cwd="/", check=False).stdout
        info["head"] = next((x[5:].strip() for x in msg.splitlines()
                             if x.startswith("HEAD ")), "")
    return info["head"]


def _tip_time(st):
    r = git("--git-dir", str(st), "log", "-1", "--format=%ct", RECORD_REF,
            cwd="/", check=False).stdout.strip()
    return int(r) if r.isdigit() else 0


def _prune_empty(d):
    root = stores_root()
    while d != root and d.is_relative_to(root):
        try:
            d.rmdir()
        except OSError:
            return
        d = d.parent


def _install_ref(st, source, ref, sha, tag):
    """Copy commit sha from the repo or store at source into store st as ref.

    If st already has ref at another commit, the new one goes in beside it
    as ref-<tag>-<time>-<sha>. Fetched to a private ref first, then
    installed only if the target is still what we saw (compare-and-swap),
    so a concurrent snapshot is never overwritten. Returns the ref used.
    """
    have = git("--git-dir", str(st), "rev-parse", "-q", "--verify", ref,
               cwd="/", check=False).stdout.strip()
    if have == sha:
        return ref
    dest = ref
    if have:
        dest = f"{ref}-{tag}-{time.strftime('%Y%m%dT%H%M%S')}-{sha[:8]}"
    tmp = f"refs/awa/tmp/{os.getpid()}-{sha[:8]}"
    git("--git-dir", str(st), "fetch", "-q", str(source), f"+{sha}:{tmp}",
        cwd="/", env={"GIT_CONFIG_COUNT": "1",  # fetch the object by id
                      "GIT_CONFIG_KEY_0": "uploadpack.allowAnySHA1InWant",
                      "GIT_CONFIG_VALUE_0": "true"})
    try:
        git("--git-dir", str(st), "update-ref", dest, sha,
            have if dest == ref else "0" * 40, cwd="/")
    finally:
        git("--git-dir", str(st), "update-ref", "-d", tmp, cwd="/",
            check=False)
    return dest


def _recorded_refs(git_args):
    out = git(*git_args, "for-each-ref", "--format=%(objectname) %(refname)",
              RECORD_REF, f"{RECORD_REF}-*", cwd="/").stdout
    return [tuple(x.split(" ", 1)) for x in out.splitlines() if x]


def merge_store(src, st):
    """Copy every recorded ref of store src into store st, never replacing
    one, then delete src once st holds them all."""
    refs = _recorded_refs(["--git-dir", str(src)])
    kept = [(_install_ref(st, src, ref, sha, "moved"), sha)
            for sha, ref in refs]
    for ref, sha in kept:
        if git("--git-dir", str(st), "rev-parse", "-q", "--verify", ref,
               cwd="/", check=False).stdout.strip() != sha:
            raise RuntimeError(f"{ref} did not reach {st}; kept {src}")
    shutil.rmtree(src)


def claim_store(repo, st, create=True):
    """Give repo its store st, carrying over history recorded before the
    repo was moved here.

    A repo is tied to its store by the id in <git dir>/awa/id, which the
    store keeps as awa.id. A store whose repo is gone from its path (or
    replaced there by a repo with another id) and that has repo's id is
    repo's own history: it is renamed to st, or merged into st if st
    exists. A store at st that has another repo's id (that repo was moved
    away and this one took its place) is first renamed aside, for that repo
    to claim. Stores of repos nested in the moved one follow it. A store from
    before awa kept ids matches when repo's HEAD reflog, and no other
    recorded repo's, holds the commit its last snapshot was taken at. A copy
    of a recorded repo (both still present) gets an id of its own. Returns
    the repo's id, or None if a store it should take over was left behind,
    so that a later call tries again.
    """
    repo = Path(os.path.realpath(repo))
    gd = git_dir(repo)
    idx = store_index()
    rid = read_id(gd)
    if rid and any(s != str(st) and i.get("id") == rid and _live(i)
                   for s, i in idx.items()):
        rid = None  # a copy whose original is still recorded
    rid = rid or new_id(gd)
    ok = True
    if (st / "HEAD").exists():
        here = _store_info(st)
        if here.get("id") and here["id"] != rid:
            aside = st.with_name(f"{st.name[:-4]}@awa-moved-{here['id'][:8]}"
                                 f"-{time.strftime('%Y%m%dT%H%M%S')}.git")
            try:
                os.rename(st, aside)
            except OSError as e:  # never write into another repo's history
                raise RuntimeError(f"could not set aside {st}: {e}") from e
            idx.pop(str(st), None)
            idx[str(aside)] = here
            print(f"awa: {repo}: {st} holds the history of another repo "
                  f"once at this path; kept it as {aside}", flush=True)
    reflogs = {}

    def in_reflog(path, head):
        if path not in reflogs:
            reflogs[path] = set(git("reflog", "show", "--format=%H", "HEAD",
                                    cwd=path, check=False).stdout.split())
        return head in reflogs[path]

    def ours(s, i):
        if s == str(st) or not i.get("path"):
            return False
        if i.get("id"):
            return i["id"] == rid and not _live(i)
        if _live(i):
            return False
        head = _last_head(s, i)
        if not head or not in_reflog(repo, head):
            return False
        others = [i2["path"] for i2 in list(idx.values()) if i2.get("path")
                  and i2["path"] != str(repo) and _live(i2)]
        if any(in_reflog(o, head) for o in others):
            print(f"awa: {repo}: not taking over {s}: other repos share its "
                  "last HEAD", flush=True)
            return False
        return True

    found = sorted((s for s, i in idx.items() if ours(s, i)),
                   key=lambda s: _tip_time(s), reverse=True)
    for s in found:
        if s not in idx or not os.path.exists(s):
            continue  # taken by a nested repo's claim
        old = idx[s].get("path")
        nested = True  # repos nested in the moved one follow it first
        for s2, i2 in list(idx.items()):
            p2 = i2.get("path") or ""
            if s2 in idx and p2.startswith(old + os.sep) and not _live(i2):
                inner = repo / os.path.relpath(p2, old)
                if (inner / ".git").exists():
                    nested &= bool(claim_store(inner, store_dir(inner),
                                               create=False))
        if not nested:  # keep the old parent store, so a retry finds them
            ok = False
            continue
        try:
            if (st / "HEAD").exists():
                merge_store(Path(s), st)
                print(f"awa: {repo}: merged the history recorded at {old} "
                      f"into {st}", flush=True)
            else:
                st.parent.mkdir(parents=True, exist_ok=True)
                os.rename(s, st)
                print(f"awa: {repo}: moved the history recorded at {old} "
                      f"to {st}", flush=True)
        except (RuntimeError, OSError) as e:
            print(f"awa: {repo}: could not take over {s}: {e}", flush=True)
            ok = False
            continue
        idx.pop(s)
        _prune_empty(Path(s).parent)
    if not (st / "HEAD").exists():
        if not create:
            return rid if ok else None
        st.parent.mkdir(parents=True, exist_ok=True)
        tmp = st.with_name(f"{st.name}.tmp{os.getpid()}")
        shutil.rmtree(tmp, ignore_errors=True)
        git("init", "-q", "--bare", str(tmp), cwd="/")
        os.rename(tmp, st)
    info = idx.setdefault(str(st), {})
    if info.get("path") != str(repo) or info.get("id") != rid:
        git("config", "awa.path", str(repo), cwd=st)
        git("config", "awa.id", rid, cwd=st)
        info.update(path=str(repo), id=rid)
        info.pop("head", None)
    return rid if ok else None


def open_store(repo):
    """Return repo's store, creating it or taking over the one the repo had
    at a previous path (see claim_store) the first time this process opens
    it or finds another repo there, and move in any history still kept in the repo itself
    (refs/awa/record*, from older versions of awa)."""
    st = store_dir(repo)
    key = (str(stores_root()), str(st))
    if _checked.get(key) != path_id(repo) or not (st / "HEAD").exists():
        stores_root().mkdir(parents=True, exist_ok=True)
        with open(stores_root() / ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)  # one claim at a time
            rid = claim_store(repo, st)
            if rid:
                _checked[key] = rid
    for sha, ref in _recorded_refs(["-C", str(repo)]):
        _install_ref(st, repo, ref, sha, "imported")
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
    refs = git("--git-dir", str(src), "for-each-ref", "--format=%(refname)",
               RECORD_REF, f"{RECORD_REF}-*", cwd="/").stdout.split()
    if refs:
        kept = f"{RECORD_REF}-{name}-{time.strftime('%Y%m%dT%H%M%S')}"
        while git("--git-dir", str(st), "for-each-ref", f"{kept}", f"{kept}-*",
                  cwd="/").stdout.strip():
            kept += "x"
        git("--git-dir", str(st), "fetch", "-q", str(src),
            *[f"{r}:{kept}{r[len(RECORD_REF):]}" for r in refs], cwd="/")
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


# ---------------------------------------------------------------- backup

BACKUP_TAG = "awa"
# restic forget policy; AWA_BACKUP_KEEP in backup.env replaces it
BACKUP_KEEP = ("--keep-hourly 48 --keep-daily 30 --keep-weekly 26 "
               "--keep-monthly 24")
BACKUP_TRIES = 5            # copies of a store that fails its check
BACKUP_RETRY_SECONDS = 1.0  # wait before copying it again
BACKUP_UNITS = ("awa-backup.service", "awa-backup.timer",
                "awa-backup-prune.service", "awa-backup-prune.timer")


def state_home():
    return Path(os.environ.get("XDG_STATE_HOME")
                or Path.home() / ".local" / "state")


def backup_config():
    return config_home() / "awa" / "backup.env"


def backup_stage():
    """Where `awa backup` puts the checked copy restic reads. Fixed, so
    restic finds its parent snapshot and only reads changed files."""
    return state_home() / "awa" / "backup-stage"


def read_env_file(path):
    """KEY=VALUE lines (an `export ` prefix, quotes and # comments allowed)
    as a dict. Values may be secrets: never print them."""
    out = {}
    for n, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if not sep or not key.isidentifier():
            raise RuntimeError(f"{path}:{n}: expected KEY=VALUE")
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        out[key] = val
    return out


def backup_env():
    """Variables for restic: ~/.config/awa/backup.env, plus the provider
    credentials file it names in AWA_BACKUP_ENV_FILE (backup.env wins)."""
    path = backup_config()
    if not path.exists():
        raise RuntimeError(f"no backup config; create {path} "
                           "(see the README, Backups)")
    cfg = read_env_file(path)
    for k in ("RESTIC_PASSWORD_FILE", "AWA_BACKUP_ENV_FILE",
              "AWA_BACKUP_RESTIC"):
        if cfg.get(k):
            cfg[k] = os.path.expanduser(cfg[k])
    if cfg.get("AWA_BACKUP_ENV_FILE"):
        cfg = {**read_env_file(cfg["AWA_BACKUP_ENV_FILE"]), **cfg}
    if not cfg.get("RESTIC_REPOSITORY"):
        raise RuntimeError(f"{path}: RESTIC_REPOSITORY is not set")
    if not any(cfg.get(k) for k in ("RESTIC_PASSWORD_FILE", "RESTIC_PASSWORD",
                                    "RESTIC_PASSWORD_COMMAND")):
        raise RuntimeError(f"{path}: RESTIC_PASSWORD_FILE is not set")
    return cfg


def backup_host(cfg):
    return cfg.get("AWA_BACKUP_HOST") or socket.gethostname()


def restic(cfg, *args, quiet=False):
    """Run restic with cfg in its environment (secrets never go on the
    command line). Returns its exit code."""
    exe = cfg.get("AWA_BACKUP_RESTIC") or "restic"
    out = subprocess.DEVNULL if quiet else None
    try:
        return subprocess.run([exe, *args], env={**os.environ, **cfg},
                              stdout=out, stderr=out).returncode
    except FileNotFoundError:
        raise RuntimeError(f"{exe} not found; install restic")


def backup_lock(wait):
    """The lock that keeps backups and prunes from overlapping, or None if
    it is taken and wait is False."""
    p = state_home() / "awa" / "backup.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    f = open(p, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
    except BlockingIOError:
        f.close()
        return None
    return f


def copy_tree(src, dest):
    """Replace dest with a copy of src (reflinks where the filesystem has
    them). Files that vanish mid-copy are fine: the copy is checked."""
    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["cp", "-a", "--reflink=auto", "--", str(src),
                        str(dest)], capture_output=True, text=True)
    if not dest.exists():
        raise RuntimeError(f"copying {src} failed: {r.stderr.strip()}")


def store_dirs(root):
    """The history stores (bare repos named *.git) under root."""
    found = []
    for d, subdirs, files in os.walk(root):
        if d.endswith(".git") and "HEAD" in files and "objects" in subdirs:
            found.append(Path(d))
            subdirs[:] = []
    return sorted(found)


def store_problem(st):
    """Why the store at st is broken ('' if it isn't): any ref or reflog
    entry naming an object it doesn't have."""
    r = git("--git-dir", str(st), "fsck", "--connectivity-only",
            "--no-dangling", "--no-progress", cwd="/", check=False)
    if r.returncode == 0:
        return ""
    lines = (r.stderr or r.stdout).strip().splitlines()
    return lines[0] if lines else f"git fsck exit {r.returncode}"


def store_refs(st):
    """The refs of the store at st with their object ids, or None if it
    can't be read (gone)."""
    r = git("--git-dir", str(st), "for-each-ref",
            "--format=%(objectname) %(refname)", cwd="/", check=False)
    return r.stdout if r.returncode == 0 else None


def stage_stores(root, stage):
    """Copy each store under root to stage, as it was at one moment.

    Stores change while they are copied: a snapshot writes objects, then
    moves its ref; `git gc` writes a new pack, then deletes what it
    replaced, and packs loose refs into packed-refs, then deletes them. A
    copy can catch a ref without its objects, miss a pack, or miss a ref.
    So a copy is kept only when its refs equal the store's just before and
    just after copying it, and `git fsck --connectivity-only` finds every
    object they need; otherwise the store is copied again. Leftover lock
    files are dropped from copies. Returns [(store, problem)] for stores
    with no such copy after BACKUP_TRIES tries; they are staged as copied.
    """
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    bad = []
    for src in store_dirs(root):
        rel = src.relative_to(root)
        dest = stage / rel
        for attempt in range(1, BACKUP_TRIES + 1):
            before = store_refs(src)
            try:
                copy_tree(src, dest)
            except RuntimeError:
                if (src / "HEAD").exists():
                    raise
            if not (src / "HEAD").exists():
                shutil.rmtree(dest, ignore_errors=True)  # removed meanwhile
                break
            for f in dest.rglob("*.lock"):
                f.unlink(missing_ok=True)
            if before is None:
                why = "git can't read its refs"
            elif store_refs(src) != before:
                why = "its refs changed while it was copied"
            elif store_refs(dest) != before:
                why = "the copy's refs differ from the store's"
            else:
                why = store_problem(dest)
            if not why:
                break
            if attempt == BACKUP_TRIES:
                bad.append((rel, why))
                break
            time.sleep(BACKUP_RETRY_SECONDS)
    return bad


def backup_run():
    """Back up every history store with restic, from a checked copy."""
    cfg = backup_env()
    root = data_home() / "awa"
    if not root.is_dir():
        raise RuntimeError(f"no history stores in {root}")
    lock = backup_lock(wait=False)
    if lock is None:
        print("awa: another awa backup or prune is running; skipping")
        return 0
    with lock:
        stage = backup_stage()
        try:
            bad = stage_stores(root, stage)
            args = ["backup", "--tag", BACKUP_TAG, "--host", backup_host(cfg),
                    "--ignore-inode", "--skip-if-unchanged"]
            code = restic(cfg, *args, str(stage))
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    for rel, why in bad:
        print(f"awa: no consistent copy of {root / rel} ({why}); backed "
              "it up as copied", file=sys.stderr)
    if code != 0:
        print(f"awa: restic backup failed (exit {code})", file=sys.stderr)
    return 1 if code or bad else 0


def backup_prune():
    """Forget old awa snapshots of this host and prune, without ever
    downloading pack data: nothing is repacked, and only packs nothing
    uses are deleted."""
    cfg = backup_env()
    keep = (cfg.get("AWA_BACKUP_KEEP") or BACKUP_KEEP).split()
    with backup_lock(wait=True):
        return restic(cfg, "forget", "--prune", "--host", backup_host(cfg),
                      "--tag", BACKUP_TAG, *keep, "--max-unused", "unlimited",
                      "--repack-cacheable-only", "--max-repack-size", "0")


def backup_init():
    """Create the restic repository unless it exists."""
    cfg = backup_env()
    code = restic(cfg, "cat", "config", quiet=True)
    if code == 0:
        print(f"awa: {cfg['RESTIC_REPOSITORY']} already exists")
        return 0
    if code != 10:  # 10: the repository does not exist
        raise RuntimeError(f"can't tell whether {cfg['RESTIC_REPOSITORY']} "
                           f"exists (restic exit {code})")
    return restic(cfg, "init")


def backup_unit_files():
    """{file name: text} of the backup units: hourly backup, weekly prune."""
    cmd = shlex.join([sys.executable, os.path.abspath(__file__), "backup"])
    path = os.environ.get("PATH") or "/usr/bin:/bin"

    def service(desc, arg, timeout):
        return (f"[Unit]\nDescription=awa: {desc}\n\n[Service]\n"
                f"Type=oneshot\nEnvironment=\"PATH={path}\"\n"
                f"ExecStart={cmd}{arg}\nNice=10\nIOSchedulingClass=idle\n"
                f"TimeoutStartSec={timeout}\n")

    def timer(desc, when, delay):
        return (f"[Unit]\nDescription=awa: {desc}\n\n[Timer]\n"
                f"OnCalendar={when}\nRandomizedDelaySec={delay}\n"
                "Persistent=true\n\n[Install]\nWantedBy=timers.target\n")
    return dict(zip(BACKUP_UNITS, (
        service("back up the history stores with restic", "", "6h"),
        timer("hourly backup of the history stores", "hourly", "5min"),
        service("forget old backups and prune (no pack downloads)",
                " --prune", "2h"),
        timer("weekly prune of the history store backups", "weekly", "1h"))))


def systemctl(*args):
    r = subprocess.run(["systemctl", "--user", *args], capture_output=True,
                       text=True)
    if r.returncode != 0:
        raise RuntimeError(f"systemctl --user {' '.join(args)} failed: "
                           f"{r.stderr.strip()}")


def backup_start():
    backup_env()  # refuse to schedule backups that can't run
    d = config_home() / "systemd" / "user"
    d.mkdir(parents=True, exist_ok=True)
    for name, text in backup_unit_files().items():
        _write_if_changed(d / name, text)
    systemctl("daemon-reload")
    systemctl("enable", "--now", *[u for u in BACKUP_UNITS
                                   if u.endswith(".timer")])
    print("awa: backups scheduled: hourly (awa-backup.timer), prune weekly "
          "(awa-backup-prune.timer); logs: journalctl --user -u awa-backup")


def backup_stop():
    timers = [u for u in BACKUP_UNITS if u.endswith(".timer")]
    subprocess.run(["systemctl", "--user", "disable", "--now", *timers],
                   capture_output=True)
    d = config_home() / "systemd" / "user"
    for name in BACKUP_UNITS:
        (d / name).unlink(missing_ok=True)
    systemctl("daemon-reload")
    print("awa: backups unscheduled (a backup already running finishes)")


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

    sub.add_parser("log", help="git log refs/awa/record in the repo's store; "
                   "other arguments go to git log")

    sub.add_parser("store", help="print the path of the repo's history store")

    d = sub.add_parser("rm", help="delete a copy whose work is all merged")
    d.add_argument("name")
    d.add_argument("--force", action="store_true")

    u = sub.add_parser("update", help="update awa now from its git upstream")
    u.add_argument("--quiet", action="store_true")
    u.add_argument("--auto", action="store_true", help=argparse.SUPPRESS)

    b = sub.add_parser("backup", allow_abbrev=False,
                       help="back up every history store with restic "
                       "(settings: ~/.config/awa/backup.env)")
    g = b.add_mutually_exclusive_group()
    g.add_argument("--prune", action="store_true",
                   help="forget old backups and prune, downloading no packs")
    g.add_argument("--init", action="store_true",
                   help="create the restic repository")
    g.add_argument("--start", action="store_true",
                   help="back up hourly and prune weekly (systemd timers)")
    g.add_argument("--stop", action="store_true", help="stop the timers")
    g.add_argument("--restic", action="store_true",
                   help="run restic with the backup settings; every "
                   "argument after --restic goes to restic")

    argv = list(sys.argv[1:] if argv is None else argv)
    restic_args = []
    i = 0  # the subcommand: the first word that isn't an option or -C's value
    while i < len(argv) and argv[i].startswith("-"):
        i += 2 if argv[i] == "-C" else 1
    if argv[i:i + 1] == ["backup"] and "--restic" in argv[i:]:
        i = argv.index("--restic", i) + 1
        argv, restic_args = argv[:i], argv[i:]  # verbatim, even -- and -h
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
        elif a.cmd == "backup":
            if a.restic:
                return restic(backup_env(), *restic_args)
            if a.start:
                backup_start()
            elif a.stop:
                backup_stop()
            elif a.init:
                return backup_init()
            elif a.prune:
                return backup_prune()
            else:
                return backup_run()
        elif a.cmd == "log":
            st = open_store(toplevel(a.repo))
            return subprocess.run(["git", "--git-dir", str(st), "log",
                                   RECORD_REF, *extra]).returncode
    except RuntimeError as e:
        die(str(e))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
