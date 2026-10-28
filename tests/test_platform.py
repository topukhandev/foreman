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
