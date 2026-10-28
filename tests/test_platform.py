"""The platform layer (oscompat) and what depends on it, exercised with real separate processes (fake_cli.py) so
Linux, macOS and Windows CI each test their own locks, process trees and output decoding. Never skipped."""

import os
import subprocess
import sys
import tempfile
import time
import unittest

import helpers  # noqa: F401  (puts src/ on the path)
import platform_skips

from orchestrator.oscompat import FileLock, pid_alive
from orchestrator.util import atomic_write, read_json, run_process, run_streaming

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_cli.py")


def wait_until(cond, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


class FileLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="foreman_platform_")
        self.path = os.path.join(self.tmp, "locks", "p.lock")

    def _holder(self, task="7"):
        proc = subprocess.Popen([sys.executable, FAKE, "hold-lock", self.path, task], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait()))
        self.assertEqual(proc.stdout.readline().strip(), "ready")
        return proc

    def test_held_by_another_process_until_it_dies_and_its_details_stay_readable(self):
        proc = self._holder("7")
        lock = FileLock(self.path)
        self.assertFalse(lock.try_acquire())
        self.assertEqual(FileLock.read_info(self.path), {"pid": proc.pid, "task": "7"})
        proc.kill()
        proc.wait()
        self.assertTrue(wait_until(lock.try_acquire), "the OS releases a dead holder's lock: never stale")
        lock.release()

    def test_exclusive_within_one_process_too(self):
        a, b = FileLock(self.path), FileLock(self.path)
        self.assertTrue(a.try_acquire())
        self.assertFalse(b.try_acquire())
        self.assertFalse(b.acquire(timeout=0.2))
        a.release()
        self.assertTrue(b.acquire(timeout=1))
        b.release()

    def test_info_is_replaced_and_cleared(self):
        with FileLock(self.path) as lock:
            lock.write_info({"task": "1", "pid": 1, "note": "a much longer first value"})
            lock.write_info({"task": "2"})
            self.assertEqual(FileLock.read_info(self.path), {"task": "2"})
            lock.clear_info()
        self.assertEqual(FileLock.read_info(self.path), {})


class ProcessTest(unittest.TestCase):
    def test_pid_alive(self):
        self.assertTrue(pid_alive(os.getpid()))
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.assertFalse(pid_alive(proc.pid))
        self.assertFalse(pid_alive(0))

    def test_cli_output_is_decoded_as_utf8_whatever_the_code_page(self):
        lines = []
        res = run_streaming([sys.executable, FAKE, "stream"], cwd=tempfile.gettempdir(), timeout=60,
                            on_line=lines.append)
        self.assertEqual(res.code, 0, res.err)
        self.assertIn("✔ Größe — 日本語 — ünïcödé", res.out)
        self.assertEqual(len(lines), 3)
        res = run_process([sys.executable, FAKE, "stream"], cwd=tempfile.gettempdir(), timeout=60)
        self.assertIn("日本語", res.out)

    def test_a_timeout_ends_the_whole_process_tree(self):
        pidfile = os.path.join(tempfile.mkdtemp(prefix="foreman_tree_"), "grandchild.pid")
        start = time.time()
        res = run_process([sys.executable, FAKE, "tree", pidfile], cwd=tempfile.gettempdir(), timeout=3)
        self.assertTrue(res.timed_out)
        self.assertLess(time.time() - start, 60)
        self.assertTrue(os.path.exists(pidfile), "the grandchild had started")
        with open(pidfile) as f:
            grandchild = int(f.read())
        self.assertTrue(wait_until(lambda: not pid_alive(grandchild)), "the grandchild was killed with its parent")


class AtomicWriteTest(unittest.TestCase):
    def test_replacing_a_file_while_another_reader_has_it_open(self):
        import threading
        path = os.path.join(tempfile.mkdtemp(prefix="foreman_atomic_"), "task.json")
        atomic_write(path, '{"rev": 1}')
        opened, seen = threading.Event(), []

        def reader():  # a UI or a status call reading task.json at that moment (Windows refuses to replace it then)
            with open(path, encoding="utf-8") as f:
                opened.set()
                time.sleep(0.3)
                seen.append(f.read())
        t = threading.Thread(target=reader)
        t.start()
        opened.wait(5)
        atomic_write(path, '{"rev": 2}')
        t.join()
        self.assertEqual(seen, ['{"rev": 1}'])
        self.assertEqual(read_json(path), {"rev": 2})
        self.assertEqual([n for n in os.listdir(os.path.dirname(path)) if n.startswith(".tmp_")], [])


class PathTest(unittest.TestCase):
    def test_inside_a_folder_whatever_the_separators(self):
        from orchestrator.oscompat import is_within
        root = os.path.join(tempfile.gettempdir(), "projects", "api")
        self.assertTrue(is_within(root, root))
        self.assertTrue(is_within(os.path.join(root, "src", "x.js"), root))
        self.assertTrue(is_within(root + os.sep + "src" + os.sep, root + os.sep))
        self.assertFalse(is_within(root + "-v2", root), "a sibling with a longer name is not inside")
        self.assertFalse(is_within(os.path.dirname(root), root))
        if os.name == "nt":
            self.assertTrue(is_within(root.upper() + "\\src", root.lower()), "Windows paths ignore case")

    def test_check_commands_cannot_cd_out_of_the_project(self):
        from orchestrator.engine import check_command_allowed
        for target in ("/etc", "..", "../x", "a/../../b", "~/x", "C:\\Windows", "c:/x", "\\\\server\\share", "..\\x"):
            ok, why = check_command_allowed(f"cd {target} && npm test")
            self.assertFalse(ok, target)
        self.assertTrue(check_command_allowed("cd packages/api && npm test")[0])


class WindowsCommandTest(unittest.TestCase):
    def test_batch_file_arguments_that_cmd_would_interpret_are_refused(self):
        from orchestrator.oscompat import batch_args_problem
        self.assertEqual(batch_args_problem(["test", "--watch=false", "src/a b.js"]), "")
        for bad in ("a & calc", "x|y", "100%", "!x!", 'say "hi"', "two\nlines", "a^b", "<in"):
            self.assertTrue(batch_args_problem(["run", bad]), bad)

    def test_a_batch_file_cli_gets_its_prompt_through_a_file_only(self):
        from orchestrator.config import ProviderSettings
        from orchestrator.oscompat import batch_args_problem
        from orchestrator.providers.base import RunRequest
        from orchestrator.providers.generic import GenericProvider
        p = GenericProvider(ProviderSettings("codex", command="codex", extra={"read_args": ["exec", "{prompt}"]}))
        p._binary = r"C:\Users\me\AppData\Roaming\npm\codex.cmd"
        self.assertTrue(p.batch_shim)
        prompt = 'Fix "totals" & refunds: 100% | <b> ^done^ !now!'
        cmd = p.build_command(RunRequest(prompt, tempfile.gettempdir(), schema={"type": "object"},
                                         prompt_dir=tempfile.mkdtemp(prefix="foreman_batch_")))
        self.assertEqual(batch_args_problem(cmd[1:]), "", "nothing cmd.exe would misread on the command line")
        path = cmd[-1].rsplit(" ", 1)[-1]
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn(prompt, text)
        self.assertIn('"type"', text, "the schema instruction is in the file too")

    @unittest.skipUnless(os.name == "nt", "Windows program lookup")
    def test_programs_are_found_like_a_shell_finds_them(self):
        import shutil
        if not shutil.which("npm"):
            self.skipTest("npm is not installed")
        res = run_process(["npm", "--version"], cwd=tempfile.gettempdir(), timeout=120)  # npm is npm.cmd
        self.assertEqual(res.code, 0, res.err)
        res = run_process(["npm", "run", "x & calc"], cwd=tempfile.gettempdir(), timeout=60)
        self.assertIn("refused to run", res.err)


class GitHeadTest(unittest.TestCase):
    """HEAD read from the .git files must always equal what git itself says."""

    def setUp(self):
        from helpers import git, make_repo
        self.git = git
        self.repo = make_repo(tempfile.mkdtemp(prefix="foreman_head_"), "repo", {"a.txt": "a\n"})

    def same(self, path):
        from orchestrator.util import git_head, is_git_repo
        truth = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], capture_output=True, text=True)
        self.assertEqual(git_head(path), truth.stdout.strip() if truth.returncode == 0 else None)
        self.assertTrue(is_git_repo(path))

    def commit(self, path, name):
        with open(os.path.join(path, name), "w") as f:
            f.write(name)
        self.git(path, "add", "-A")
        self.git(path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", name)

    def test_branch_packed_refs_detached_and_unborn(self):
        self.same(self.repo)
        self.commit(self.repo, "b.txt")
        self.same(self.repo)
        self.git(self.repo, "pack-refs", "--all")  # the branch now lives only in packed-refs
        self.same(self.repo)
        self.commit(self.repo, "c.txt")
        self.same(self.repo)
        self.git(self.repo, "checkout", "-q", "--detach")
        self.same(self.repo)
        empty = os.path.join(os.path.dirname(self.repo), "empty")
        os.makedirs(empty)
        self.git(empty, "init", "-q")
        self.same(empty)  # no commit yet: no HEAD

    def test_worktrees_and_subfolders(self):
        wt = os.path.join(os.path.dirname(self.repo), "wt")
        self.git(self.repo, "worktree", "add", "-q", "-b", "side", wt)
        self.commit(wt, "w.txt")
        self.same(wt)
        self.same(self.repo)
        sub = os.path.join(self.repo, "pkg")
        os.makedirs(sub)
        self.same(sub)  # a project inside a repository: asked of git

    def test_not_a_repository(self):
        from orchestrator.util import git_head, is_git_repo
        plain = tempfile.mkdtemp(prefix="foreman_plain_")
        self.assertIsNone(git_head(plain))
        self.assertFalse(is_git_repo(plain))


class LoginEnvTest(unittest.TestCase):
    """The desktop app's Foreman reads PATH and keys from the login shell (an app started from the desktop has none)."""

    def shell(self, body: str) -> str:
        path = os.path.join(tempfile.mkdtemp(prefix="foreman_shell_"), "fakesh")
        with open(path, "w") as f:  # called as: fakesh -l -i -c '<script>'
            f.write("#!/bin/sh\n" + body + '\neval "$4"\n')
        os.chmod(path, 0o755)
        return path

    @unittest.skipIf(os.name == "nt", "Windows programs get the user's environment")
    def test_reads_the_environment_the_profile_sets_and_ignores_its_noise(self):
        from orchestrator.app import login_shell_env
        sh = self.shell("echo 'Welcome! nvm loading...'\nexport FROM_PROFILE='a=b c'\nexport PATH=/opt/fromrc/bin:$PATH")
        env = login_shell_env(timeout=20, shell=sh)
        self.assertEqual(env.get("FROM_PROFILE"), "a=b c")
        self.assertTrue(env["PATH"].startswith("/opt/fromrc/bin"))
        self.assertNotIn("PWD", env)

    @unittest.skipIf(os.name == "nt", "Windows programs get the user's environment")
    def test_a_shell_that_hangs_or_fails_gives_nothing(self):
        from orchestrator.app import login_shell_env
        self.assertEqual(login_shell_env(timeout=1, shell=self.shell("sleep 30")), {})
        self.assertEqual(login_shell_env(timeout=5, shell="/no/such/shell"), {})

    def test_merging_puts_the_profile_path_first_and_never_overrides(self):
        from orchestrator.app import apply_login_env
        sep = os.pathsep
        environ = {"PATH": sep.join(["/usr/bin", "/bin"]), "HOME": "/home/me"}
        added = apply_login_env({"PATH": sep.join(["/opt/node/bin", "/usr/bin"]), "HOME": "/elsewhere",
                                 "ANTHROPIC_API_KEY": "k"}, environ)
        self.assertEqual(environ["PATH"].split(sep), ["/opt/node/bin", "/usr/bin", "/bin"])
        self.assertEqual(environ["HOME"], "/home/me", "what the app already has is kept")
        self.assertEqual(environ["ANTHROPIC_API_KEY"], "k")
        self.assertEqual(sorted(added), ["ANTHROPIC_API_KEY", "PATH"])


class SkipListTest(unittest.TestCase):
    def test_the_skip_list_never_grows_and_every_entry_has_a_reason(self):
        count = sum(len(v) for v in platform_skips.SKIPS.values())
        self.assertLessEqual(count, platform_skips.MAX_SKIPS, "fix the test instead of skipping it (see platform_skips)")
        for test_id, platforms in platform_skips.SKIPS.items():
            self.assertNotIn("test_platform", test_id, "the platform layer's own tests are never skipped")
            for plat, why in platforms.items():
                self.assertIn(plat, ("win32", "darwin", "linux"))
                self.assertGreater(len(why), 20, f"{test_id}: say why it can't run on {plat}")


if __name__ == "__main__":
    unittest.main()
