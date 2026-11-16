#!/usr/bin/env python3
"""Offline tests for awa. Run: python3 test_awa.py (one file, one process)."""
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import awa  # noqa: E402

# Default to a folder under $HOME, which is often btrfs, so reflinks are used.
BASE = Path(os.environ.get("AWA_TEST_DIR", Path.home() / ".cache" / "awa-test"))


def sh(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, shell=True, check=True,
                          capture_output=True, text=True).stdout.strip()


def store(repo):
    """git command line reading repo's history store."""
    return f"git --git-dir={awa.store_dir(repo)}"


def wait_for(fn, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(0.05)
    return False


class Base(unittest.TestCase):
    def setUp(self):
        BASE.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=BASE))
        self.repo = self.tmp / "proj"
        self.repo.mkdir()
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "config")
        os.environ["XDG_DATA_HOME"] = str(self.tmp / "data")
        for k in ("AUTHOR", "COMMITTER"):
            os.environ[f"GIT_{k}_NAME"] = "t"
            os.environ[f"GIT_{k}_EMAIL"] = "t@t"
        sh("git init -q -b main && printf 'node_modules/\\n*.log\\n' > "
           ".gitignore && echo a > a.txt && git add -A && git commit -qm init",
           self.repo)

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def awa(self, *args):
        return awa.main(["-C", str(self.repo), *args])

    def ws(self, name):
        self.assertEqual(self.awa("new", name), 0)
        return Path(f"{self.repo}@{name}")

    def head(self, cwd=None):
        return sh("git rev-parse HEAD", cwd or self.repo)


class Snapshot(Base):
    def test_leaves_index_and_head_alone(self):
        head = self.head()
        (self.repo / "b.txt").write_text("b\n")
        (self.repo / "node_modules").mkdir()
        (self.repo / "node_modules" / "x.js").write_text("x")
        c1 = awa.snapshot(self.repo)
        self.assertEqual(self.head(), head)
        self.assertIn("?? b.txt", sh("git status --porcelain", self.repo))
        files = sh(f"{store(self.repo)} ls-tree -r --name-only {c1}",
                   self.repo).split()
        self.assertIn("b.txt", files)
        self.assertNotIn("node_modules/x.js", files)
        self.assertIsNone(awa.snapshot(self.repo))  # unchanged: no commit
        (self.repo / "b.txt").write_text("b2\n")
        c2 = awa.snapshot(self.repo)
        self.assertEqual(sh(f"{store(self.repo)} rev-parse {c2}^", self.repo),
                         c1)

    def test_history_lives_only_in_the_store(self):
        sh("echo s > sub.txt && git add -A && git commit -qm sub", self.repo)
        (self.repo / "b.txt").write_text("b\n")
        c = awa.snapshot(self.repo)
        self.assertEqual(sh("git for-each-ref refs/awa", self.repo), "")
        self.assertNotEqual(subprocess.run(
            ["git", "cat-file", "-e", c], cwd=self.repo).returncode, 0)
        st = awa.store_dir(self.repo)
        self.assertTrue(st.is_relative_to(self.tmp / "data" / "awa"))
        subprocess.run(["rm", "-rf", str(self.repo)], check=True)
        sh(f"git --git-dir={st} fsck --strict --no-dangling", self.tmp)
        self.assertEqual(sh(f"git --git-dir={st} show {c}:sub.txt", self.tmp),
                         "s")  # committed in the repo, still in the store

    def test_moves_old_in_repo_history_to_the_store(self):
        tree = sh("git write-tree", self.repo)
        old = sh(f"echo old | git commit-tree {tree}", self.repo)
        kept = sh(f"echo kept | git commit-tree {tree}", self.repo)
        sh(f"git update-ref {awa.RECORD_REF} {old} && git update-ref "
           f"{awa.RECORD_REF}-w1-20260101T000000 {kept}", self.repo)
        (self.repo / "b.txt").write_text("b\n")
        c = awa.snapshot(self.repo)
        self.assertEqual(sh(f"{store(self.repo)} rev-parse {c}^", self.repo),
                         old)
        self.assertEqual(sh(f"{store(self.repo)} rev-parse "
                            f"{awa.RECORD_REF}-w1-20260101T000000",
                            self.repo), kept)
        self.assertEqual(sh("git for-each-ref refs/awa", self.repo), "")

    def test_follows_tracked_and_ignore_rules(self):
        sh("echo t > tracked.log && git add -f tracked.log && "
           "git commit -qm log", self.repo)
        (self.repo / "later.txt").write_text("1\n")
        c1 = awa.snapshot(self.repo)
        files = sh(f"{store(self.repo)} ls-tree -r --name-only {c1}",
                   self.repo).split()
        self.assertIn("tracked.log", files)  # tracked despite *.log
        self.assertIn("later.txt", files)
        sh("echo later.txt >> .gitignore", self.repo)
        c2 = awa.snapshot(self.repo)
        files = sh(f"{store(self.repo)} ls-tree -r --name-only {c2}",
                   self.repo).split()
        self.assertNotIn("later.txt", files)  # now ignored: dropped


class Watching(Base):
    def setUp(self):
        super().setUp()
        self.w = awa.Watcher(self.repo)

    def tearDown(self):
        os.close(self.w.fd)
        super().tearDown()

    def events(self):
        return wait_for(lambda: self.w.read(), 2.0)

    def watched(self, rel):
        return str(self.repo / rel) in self.w.wd_path.values()

    def test_dirs_named_like_build_output_are_watched(self):
        sh("mkdir build && echo x > build/source.py && git add -A && "
           "git commit -qm b", self.repo)
        self.w.rescan()
        (self.repo / "build" / "source.py").write_text("y\n")
        self.assertTrue(self.events())

    def test_ignored_dir_skipped_until_unignored(self):
        (self.repo / "node_modules" / "pkg").mkdir(parents=True)
        self.events()
        self.assertFalse(self.watched("node_modules"))
        (self.repo / ".gitignore").write_text("*.log\n")
        self.assertTrue(self.events())
        self.assertTrue(self.watched("node_modules/pkg"))

    def test_new_and_moved_dirs(self):
        (self.repo / "d" / "e").mkdir(parents=True)
        self.events()
        self.assertTrue(self.watched("d/e"))
        os.rename(self.repo / "d", self.tmp / "outside")
        self.events()
        self.assertFalse(self.watched("d/e"))
        self.assertFalse(any(p.startswith(str(self.tmp / "outside"))
                             for p in self.w.wd_path.values()))
        os.rename(self.tmp / "outside", self.repo / "back")
        self.events()
        self.assertTrue(self.watched("back/e"))


class Recording(Base):
    def test_records_shell_edits(self):
        awa.QUIET_SECONDS = 0.3
        threading.Thread(target=awa.record_loop, args=(self.repo,),
                         daemon=True).start()
        self.assertTrue(wait_for(lambda: subprocess.run(
            ["git", "--git-dir", str(awa.store_dir(self.repo)), "rev-parse",
             "-q", "--verify", awa.RECORD_REF],
            capture_output=True).returncode == 0))
        start = sh(f"{store(self.repo)} rev-parse {awa.RECORD_REF}", self.repo)
        (self.repo / "sub").mkdir()
        time.sleep(0.2)
        sh("echo hi > sub/new.txt && sed -i s/a/z/ a.txt", self.repo)
        self.assertTrue(wait_for(lambda: sh(
            f"{store(self.repo)} rev-parse {awa.RECORD_REF}", self.repo) != start))
        now = sh(f"{store(self.repo)} rev-parse {awa.RECORD_REF}", self.repo)
        self.assertEqual(sh(f"{store(self.repo)} show {now}:sub/new.txt", self.repo), "hi")
        self.assertEqual(sh(f"{store(self.repo)} show {now}:a.txt", self.repo), "z")


class AwaIgnore(Base):
    def setUp(self):
        super().setUp()
        sh("mkdir -p vend/sub && echo t > vend/t.txt && echo s > vend/sub/s.txt"
           " && git add -A && git commit -qm vend", self.repo)

    def files(self, c):
        return sh(f"{store(self.repo)} ls-tree -r --name-only {c}",
                  self.repo).split()

    def watched(self, w):
        return set(w.wd_path.values())

    def test_snapshot_drops_untracked_keeps_tracked(self):
        (self.repo / ".awaignore").write_text("vend/\n")
        (self.repo / "vend" / "new.txt").write_text("n\n")
        (self.repo / "vend" / "t.txt").write_text("t2\n")
        c = awa.snapshot(self.repo)
        files = self.files(c)
        self.assertNotIn("vend/new.txt", files)
        self.assertIn(".awaignore", files)
        self.assertEqual(sh(f"{store(self.repo)} show {c}:vend/t.txt", self.repo), "t2")
        # git itself doesn't see awa's rules
        self.assertIn("?? vend/new.txt", sh("git status --porcelain", self.repo))

    def test_global_rules(self):
        cfg = Path(os.environ["XDG_CONFIG_HOME"]) / "awa"
        cfg.mkdir(parents=True)
        (cfg / "ignore").write_text("scratch/\n")
        (self.repo / "scratch").mkdir()
        (self.repo / "scratch" / "x").write_text("x")
        self.assertNotIn("scratch/x", self.files(awa.snapshot(self.repo)))

    def test_gitignore_can_reinclude(self):
        (self.repo / ".awaignore").write_text("*.tmp\n")
        sh("echo '!keep.tmp' >> .gitignore", self.repo)
        (self.repo / "keep.tmp").write_text("k")
        (self.repo / "drop.tmp").write_text("d")
        files = self.files(awa.snapshot(self.repo))
        self.assertIn("keep.tmp", files)
        self.assertNotIn("drop.tmp", files)

    def test_watcher_skips_matched_dirs_even_if_tracked(self):
        (self.repo / ".awaignore").write_text("vend/\n")
        w = awa.Watcher(self.repo)
        try:
            self.assertIn(str(self.repo), self.watched(w))
            self.assertNotIn(str(self.repo / "vend"), self.watched(w))
            self.assertNotIn(str(self.repo / "vend" / "sub"), self.watched(w))
            (self.repo / ".awaignore").write_text("")
            self.assertTrue(wait_for(lambda: w.read(), 2.0))
            self.assertIn(str(self.repo / "vend" / "sub"), self.watched(w))
        finally:
            w.close()

    def test_dir_matched_by_both_rule_files_is_skipped(self):
        sh("echo 'vend/' >> .gitignore && git commit -qam g", self.repo)
        (self.repo / ".awaignore").write_text("vend/\n")
        w = awa.Watcher(self.repo)
        try:
            self.assertNotIn(str(self.repo / "vend"), self.watched(w))
        finally:
            w.close()

    def test_gitignored_dir_holding_tracked_files_is_watched(self):
        sh("echo 'gen/' >> .gitignore && mkdir -p gen/in && echo g > gen/g.txt"
           " && git add -f gen/g.txt .gitignore && git commit -qm g", self.repo)
        (self.repo / "node_modules" / "pkg").mkdir(parents=True)
        w = awa.Watcher(self.repo)
        try:
            self.assertIn(str(self.repo / "gen"), self.watched(w))
            self.assertNotIn(str(self.repo / "node_modules"), self.watched(w))
        finally:
            w.close()


class Tree(Base):
    def make_repo(self, rel):
        d = self.tmp / rel
        d.mkdir(parents=True)
        sh("git init -q -b main && echo a > a.txt && git add -A && "
           "git commit -qm init", d)
        return d

    def ref(self, d):
        return subprocess.run(["git", "--git-dir", str(awa.store_dir(d)),
                               "rev-parse", "-q", "--verify", awa.RECORD_REF],
                              capture_output=True, text=True).stdout.strip()

    def test_discover(self):
        nested = self.make_repo("group/inner")
        self.make_repo("a/b/c/d/e")  # deeper than TREE_DEPTH
        self.make_repo("proj/node_modules/pkg")  # dependency folder
        off = self.make_repo("off")
        (off / ".git" / awa.NORECORD).touch()
        sh("git worktree add -q ../wt", self.repo)  # worktrees are refused
        found = set(awa.discover(self.tmp))
        self.assertEqual(found, {os.path.realpath(self.repo),
                                 os.path.realpath(nested)})

    def test_records_every_repo_in_one_process(self):
        quiet, again = awa.QUIET_SECONDS, awa.REDISCOVER_SECONDS
        awa.QUIET_SECONDS, awa.REDISCOVER_SECONDS = 0.3, 0.5
        other = self.make_repo("other")
        stop = threading.Event()
        t = threading.Thread(target=awa.record_tree, args=(self.tmp,),
                             kwargs={"stop": stop}, daemon=True)
        t.start()
        try:
            self.assertTrue(wait_for(lambda: self.ref(self.repo)
                                     and self.ref(other)))
            first = self.ref(self.repo), self.ref(other)
            (self.repo / "x.txt").write_text("x")
            (other / "y.txt").write_text("y")
            self.assertTrue(wait_for(lambda: self.ref(self.repo) != first[0]
                                     and self.ref(other) != first[1]))
            late = self.make_repo("late")  # appears after the start
            self.assertTrue(wait_for(lambda: self.ref(late)))
            time.sleep(0.3)
            start = self.ref(late)
            (late / "z.txt").write_text("z")
            self.assertTrue(wait_for(lambda: self.ref(late) != start))
        finally:
            stop.set()
            t.join(15)
            awa.QUIET_SECONDS, awa.REDISCOVER_SECONDS = quiet, again
        self.assertFalse(t.is_alive())


class Workspaces(Base):
    def test_new_merge_sync_rm(self):
        w1, w2 = self.ws("w1"), self.ws("w2")
        self.assertEqual(sh("git branch --show-current", w1), "awa/w1")
        sh("echo one > one.txt && git add -A && git commit -qm one", w1)
        sh("echo two > two.txt && git add -A && git commit -qm two", w2)
        self.assertEqual(self.awa("merge", "w1"), 0)
        self.assertEqual(self.awa("merge", "w2"), 0)
        self.assertTrue((self.repo / "one.txt").exists())
        self.assertTrue((self.repo / "two.txt").exists())
        self.assertTrue((w2 / "one.txt").exists())  # synced after merge
        self.assertEqual(self.awa("rm", "w1"), 0)
        self.assertFalse(w1.exists())
        self.assertFalse(list(self.tmp.glob(".*merging")))

    def test_conflict_leaves_repo_unchanged(self):
        w1 = self.ws("w1")
        sh("echo mine > a.txt && git commit -qam mine", w1)
        sh("echo theirs > a.txt && git commit -qam theirs", self.repo)
        head = self.head()
        self.assertEqual(self.awa("merge", "w1"), 1)
        self.assertEqual(self.head(), head)
        self.assertEqual(sh("git status --porcelain", self.repo), "")

    def test_failed_test_changes_nothing_and_keeps_concurrent_edits(self):
        w1 = self.ws("w1")
        sh("echo bad > bad.txt && git add -A && git commit -qm bad", w1)
        sh("echo main-only > m.txt && git add -A && git commit -qm m",
           self.repo)
        head = self.head()
        # The test runs in a temporary copy; meanwhile someone edits the repo.
        cmd = f"echo concurrent > '{self.repo}/a.txt'; test ! -e bad.txt"
        self.assertEqual(self.awa("merge", "w1", "--test", cmd), 1)
        self.assertEqual(self.head(), head)
        self.assertFalse((self.repo / "bad.txt").exists())
        self.assertEqual((self.repo / "a.txt").read_text(), "concurrent\n")
        self.assertTrue((w1 / "m.txt").exists())  # synced despite failure

    def test_merge_never_overwrites_ignored_files(self):
        w1 = self.ws("w1")
        sh("echo theirs > local.log && git add -f local.log && "
           "git commit -qm l", w1)
        (self.repo / "local.log").write_text("precious\n")
        self.assertEqual(self.awa("merge", "w1"), 1)
        self.assertEqual((self.repo / "local.log").read_text(), "precious\n")
        self.assertFalse(list(self.tmp.glob(".*merging")))

    def test_rm_refuses_unique_work(self):
        w1 = self.ws("w1")
        sh("echo x > x.txt && git add -A && git commit -qm x", w1)
        with self.assertRaises(SystemExit):
            self.awa("rm", "w1")
        w2 = self.ws("w2")
        sh("git switch -q -c feature && echo f > f.txt && git add -A && "
           "git commit -qm f", w2)
        with self.assertRaises(SystemExit):
            self.awa("rm", "w2")
        w3 = self.ws("w3")
        sh("echo s >> a.txt && git stash -q", w3)
        with self.assertRaises(SystemExit):
            self.awa("rm", "w3")
        self.assertTrue(w1.exists() and w2.exists() and w3.exists())

    def test_rm_keeps_every_edit_history(self):
        tips = []
        for i in range(2):
            w1 = self.ws("w1")
            (w1 / "note.txt").write_text(f"{i}\n")
            tips.append(awa.snapshot(w1))
            (w1 / "note.txt").unlink()
            self.assertEqual(self.awa("rm", "w1"), 0)
        kept = sh(f"{store(self.repo)} for-each-ref "
                  f"--format='%(objectname)' {awa.RECORD_REF}-w1-*",
                  self.repo).split()
        self.assertEqual(sorted(kept), sorted(tips))
        self.assertFalse(awa.store_dir(w1).exists())

    def test_rm_refuses_unique_ignored_files_and_stale_branches(self):
        w1 = self.ws("w1")
        (w1 / "secret.log").write_text("s\n")
        with self.assertRaises(SystemExit):
            self.awa("rm", "w1")
        (w1 / "secret.log").unlink()
        # A branch that protected w1's commit is deleted from the repo.
        sh("echo x > x.txt && git add -A && git commit -qm x", w1)
        sh(f"git fetch -q '{w1}' awa/w1:tmp", self.repo)
        self.assertEqual(awa.unique_work(self.repo, w1), "")  # caches tmp
        sh("git branch -q -D tmp", self.repo)
        with self.assertRaises(SystemExit):
            self.awa("rm", "w1")
        self.assertTrue(w1.exists())

    def test_rm_refuses_unique_files_inside_shared_ignored_dir(self):
        sh("printf 'private/\\n' >> .gitignore && git commit -qam ig && "
           "mkdir private && echo shared > private/shared.env", self.repo)
        w1 = self.ws("w1")
        (w1 / "private" / "unique.env").write_text("u\n")
        (w1 / "node_modules" / "pkg").mkdir(parents=True)
        (w1 / "node_modules" / "pkg" / "index.js").write_text("x\n")
        with self.assertRaises(SystemExit):
            self.awa("rm", "w1")
        (w1 / "private" / "unique.env").unlink()
        self.assertEqual(self.awa("rm", "w1"), 0)  # node_modules: regenerable

    def test_rm_checks_folders_named_build_that_are_not_ignored(self):
        sh("mkdir build && echo s > build/source.py && git add -A && "
           "git commit -qm b", self.repo)
        w1 = self.ws("w1")
        (w1 / "build" / "unique.log").write_text("u\n")  # ignored by *.log
        with self.assertRaises(SystemExit):
            self.awa("rm", "w1")
        self.assertTrue((w1 / "build" / "unique.log").exists())

    def test_merge_refuses_branch_switch_at_publish(self):
        w1 = self.ws("w1")
        sh("echo x > x.txt && git add -A && git commit -qm x", w1)
        real_git = awa.git

        def git(*args, cwd, **kw):
            # Switch the repo's branch right after the candidate is fetched.
            r = real_git(*args, cwd=cwd, **kw)
            if args[:2] == ("fetch", "-q") and args[-1] == "HEAD":
                real_git("switch", "-q", "-c", "late", cwd=self.repo)
            return r
        awa.git = git
        try:
            self.assertEqual(self.awa("merge", "w1"), 1)
        finally:
            awa.git = real_git
        self.assertFalse((self.repo / "x.txt").exists())
        sh("git switch -q main", self.repo)
        self.assertFalse((self.repo / "x.txt").exists())

    def test_sync_keeps_workspace_edits(self):
        w1 = self.ws("w1")
        sh("echo w > w.txt && git add -A && git commit -qm w", w1)
        sh("echo m > m.txt && git add -A && git commit -qm m", self.repo)
        (w1 / "m.txt").write_text("agent's own\n")  # untracked in w1
        repo, dest = awa.resolve(str(self.repo), "w1")
        self.assertFalse(awa.ws_sync(repo, dest))
        self.assertEqual((w1 / "m.txt").read_text(), "agent's own\n")
        (w1 / "m.txt").unlink()
        self.assertTrue(awa.ws_sync(repo, dest))
        self.assertTrue((w1 / "m.txt").exists() and (w1 / "w.txt").exists())
        self.assertEqual(len(sh("git log --format=%p -1", w1).split()), 2)

    def test_merge_refuses_if_repo_branch_switched(self):
        w1 = self.ws("w1")
        sh("echo x > x.txt && git add -A && git commit -qm x", w1)
        cmd = f"git -C '{self.repo}' switch -q -c side"
        self.assertEqual(self.awa("merge", "w1", "--test", cmd), 1)
        self.assertFalse((self.repo / "x.txt").exists())
        sh("git switch -q main", self.repo)
        self.assertFalse((self.repo / "x.txt").exists())

    def test_leftover_candidate_still_syncs(self):
        w1 = self.ws("w1")
        sh("echo x > x.txt && git add -A && git commit -qm x", w1)
        sh("echo m > m.txt && git add -A && git commit -qm m", self.repo)
        (self.tmp / ".proj@w1.merging").mkdir()
        self.assertEqual(self.awa("merge", "w1"), 1)
        self.assertTrue((w1 / "m.txt").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
