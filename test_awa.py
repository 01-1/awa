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
        files = sh(f"git ls-tree -r --name-only {c1}", self.repo).split()
        self.assertIn("b.txt", files)
        self.assertNotIn("node_modules/x.js", files)
        self.assertIsNone(awa.snapshot(self.repo))  # unchanged: no commit
        (self.repo / "b.txt").write_text("b2\n")
        c2 = awa.snapshot(self.repo)
        self.assertEqual(sh(f"git rev-parse {c2}^", self.repo), c1)

    def test_follows_tracked_and_ignore_rules(self):
        sh("echo t > tracked.log && git add -f tracked.log && "
           "git commit -qm log", self.repo)
        (self.repo / "later.txt").write_text("1\n")
        c1 = awa.snapshot(self.repo)
        files = sh(f"git ls-tree -r --name-only {c1}", self.repo).split()
        self.assertIn("tracked.log", files)  # tracked despite *.log
        self.assertIn("later.txt", files)
        sh("echo later.txt >> .gitignore", self.repo)
        c2 = awa.snapshot(self.repo)
        files = sh(f"git ls-tree -r --name-only {c2}", self.repo).split()
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
            ["git", "rev-parse", "-q", "--verify", awa.RECORD_REF],
            cwd=self.repo, capture_output=True).returncode == 0))
        start = sh(f"git rev-parse {awa.RECORD_REF}", self.repo)
        (self.repo / "sub").mkdir()
        time.sleep(0.2)
        sh("echo hi > sub/new.txt && sed -i s/a/z/ a.txt", self.repo)
        self.assertTrue(wait_for(lambda: sh(
            f"git rev-parse {awa.RECORD_REF}", self.repo) != start))
        now = sh(f"git rev-parse {awa.RECORD_REF}", self.repo)
        self.assertEqual(sh(f"git show {now}:sub/new.txt", self.repo), "hi")
        self.assertEqual(sh(f"git show {now}:a.txt", self.repo), "z")


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
        kept = sh(f"git for-each-ref --format='%(objectname)' "
                  f"{awa.RECORD_REF}-w1-*", self.repo).split()
        self.assertEqual(sorted(kept), sorted(tips))

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
