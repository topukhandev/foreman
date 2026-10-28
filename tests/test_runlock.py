"""One run of a task at a time, across processes: the run lock, refused changes while a task runs, recovery after a
runner dies, the one-time run token, and stopping a run from another process (any OS)."""

import os
import subprocess
import sys
import textwrap
import time
import unittest
from unittest import mock

from helpers import Script, Workspace, make_engine, write

import orchestrator.engine as engine_mod

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_cli.py")
TESTS = os.path.dirname(os.path.abspath(__file__))


def plan_one_change(req):
    return {"intent": "change", "title": "Footer", "projects": [{"name": "admin-ui", "role": "modify", "why": "x"}],
            "steps": [{"id": "1", "project": "admin-ui", "kind": "implement", "instructions": "add a footer",
                       "depends_on": []}], "questions": [], "checks": []}


def edit(req):
    write(req.cwd, "src/footer.js", "// footer\n")
    return {"status": "done", "summary": "added the footer", "files_changed": ["src/footer.js"]}


class RunLockTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.s = Script()
        self.s.plan, self.s.step = plan_one_change, edit
        self.eng = make_engine(self.ws, self.s)
        self.eng.registry.ensure_scanned(self.ws.ui)  # as route() does before preparing a task

    def tearDown(self):
        self.ws.cleanup()

    def _calls(self) -> int:
        return sum(len(p.calls) for p in self.eng.providers.values())

    def _record(self, task) -> bytes:
        with open(self.eng.store.path(task["id"], "task.json"), "rb") as f:
            return f.read()

    def _hold_run_lock(self, task):
        """Another process holds the task's run lock, as a runner does for the whole run."""
        proc = subprocess.Popen([sys.executable, FAKE, "hold-lock", self.eng.store.path(task["id"], "run.lock"),
                                 task["id"]], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait()))
        self.assertEqual(proc.stdout.readline().strip(), "ready")
        return proc

    def test_a_task_another_process_runs_is_not_run_again(self):
        task = self.eng.prepare_new("add a footer", self.ws.ui)
        self._hold_run_lock(task)
        before = self._record(task)
        with mock.patch.object(engine_mod, "RUN_LOCK_WAIT", 0.2):
            res = self.eng.execute(dict(task))
        self.assertIn("already running", res["report"])
        self.assertEqual(self._calls(), 0)
        self.assertEqual(self._record(task), before, "nothing written")

    def test_changes_are_refused_while_the_task_runs(self):
        task = self.eng.store.new_task("add a footer", self.ws.ui)
        task.update(status="needs_input", questions=[{"text": "Which colour?", "kind": "business", "answer": None}])
        self.eng.store.save(task)
        self._hold_run_lock(task)
        before = self._record(task)
        t, msg = self.eng.prepare_answer(task["id"], "blue")
        self.assertIsNone(t)
        self.assertIn("is running", msg)
        t, msg = self.eng.prepare_resume(task["id"])
        self.assertIsNone(t)
        self.assertIn("is running", msg)
        self.assertIn("is running", self.eng.close(task["id"])["report"])
        self.assertEqual(self._record(task), before, "task.json unchanged")

    def test_a_dead_runner_leaves_no_lock_and_the_task_is_interrupted_then_resumable(self):
        task = self.eng.store.new_task("add a footer", self.ws.ui)
        task["status"] = "running"
        self.eng.store.save(task)
        holder = self._hold_run_lock(task)
        self.assertEqual(self.eng.status(task["id"], self.ws.ui)["task"]["status"], "running", "its runner is alive")
        holder.kill()
        holder.wait()
        self.assertEqual(self.eng.status(task["id"], self.ws.ui)["task"]["status"], "interrupted")
        ev = [e for e in self.eng.store.events(task["id"]) if e["event"] == "status"]
        self.assertEqual((ev[-1]["status"], ev[-1]["previous"], ev[-1]["reason"]), ("interrupted", "running", "runner_exited"))
        self.assertEqual(self.eng.resume(task["id"], cwd=self.ws.ui)["task"]["status"], "done")

    def test_a_queued_task_gets_time_for_its_runner_to_start(self):
        task = self.eng.prepare_new("add a footer", self.ws.ui)
        self.assertEqual(self.eng.reconcile(task)["status"], "queued")
        task = self.eng.store.load(task["id"])
        task["queued_at"] -= engine_mod.QUEUED_GRACE + 1
        self.eng.store.save(task)
        self.assertEqual(self.eng.reconcile(task)["status"], "interrupted", "its runner never came")

    def test_a_second_runner_for_the_same_request_does_nothing(self):
        task = self.eng.prepare_new("add a footer", self.ws.ui)
        duplicate = dict(task)  # e.g. the UI and an MCP client both started a runner for one request
        first = self.eng.execute(task)["task"]
        self.assertEqual((first["status"], first["steps"][0]["status"]), ("done", "done"))
        calls = self._calls()
        res = self.eng.execute(duplicate)
        self.assertIn("not waiting to run any more", res["report"])
        self.assertEqual(self._calls(), calls, "no second (paid) run")
        self.assertEqual(self.eng.store.load(task["id"])["runs"], 1)

    def test_a_change_made_after_the_caller_loaded_the_task_is_kept(self):
        task = self.eng.prepare_new("add a footer", self.ws.ui)
        other = self.eng.store.load(task["id"])  # another process changes the record before the runner starts
        other["decisions"].append({"text": "use the brand colours", "by": "user", "at": "now"})
        self.eng.store.save(other)
        done = self.eng.execute(task)["task"]
        self.assertIn("use the brand colours", [d["text"] for d in done["decisions"]])
        self.assertIs(done, task, "the caller's record is updated in place")

    def test_answers_queue_one_run_with_a_fresh_token(self):
        task = self.eng.store.new_task("add a footer", self.ws.ui)
        task.update(status="needs_input", questions=[{"text": "Which colour?", "kind": "business", "answer": None}])
        self.eng.store.save(task)
        queued, msg = self.eng.prepare_answer(task["id"], "blue")
        self.assertEqual((queued["status"], msg), ("queued", ""))
        self.assertTrue(queued["run_request"])
        self.assertEqual(self.eng.store.load(task["id"])["run_request"], queued["run_request"])

    def test_nothing_to_stop_when_nothing_runs(self):
        task = self.eng.prepare_new("add a footer", self.ws.ui)
        self.assertFalse(self.eng.request_stop(task["id"]))


CHILD = textwrap.dedent('''
    import sys, time
    sys.path.insert(0, {tests!r})
    from helpers import Script, make_engine, write
    from orchestrator.util import STOPPING

    class WS:
        home, ui = {home!r}, {ui!r}

    s = Script()
    s.plan = lambda req: {{"intent": "change", "title": "Footer", "projects": [{{"name": "admin-ui", "role": "modify",
                          "why": "x"}}], "steps": [{{"id": "1", "project": "admin-ui", "kind": "implement",
                          "instructions": "footer", "depends_on": []}}], "questions": [], "checks": []}}

    def killed_when_stopped(provider, req):  # a CLI working until the stop kills it (it then ends without a result)
        write(req.cwd, "src/footer.js", "// half done\\n")
        print("ready", flush=True)
        for _ in range(600):
            if STOPPING.is_set():
                return True
            time.sleep(0.05)
        return False
    s.fail_steps = killed_when_stopped
    eng = make_engine(WS, s)
    task = eng.store.load({task_id!r})
    task["run_request"] = {run!r}
    eng.execute(task)
''')


class StopTest(unittest.TestCase):
    """Stopping is a request any process can make on any OS (a stop file the runner watches), not a signal. Runs in a
    child process: stopping is process-wide there."""

    def setUp(self):
        self.ws = Workspace()
        self.eng = make_engine(self.ws, Script())
        self.eng.registry.ensure_scanned(self.ws.ui)  # saved, so the runner process knows the projects too

    def tearDown(self):
        self.ws.cleanup()

    def test_a_stop_request_ends_the_run_as_interrupted_and_it_can_continue(self):
        task = self.eng.prepare_new("add a footer", self.ws.ui)
        code = CHILD.format(tests=TESTS, home=self.ws.home, ui=self.ws.ui, task_id=task["id"], run=task["run_request"])
        child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (child.poll() is None and child.kill(), child.wait()))
        self.assertEqual(child.stdout.readline().strip(), "ready")
        self.assertTrue(self.eng.is_running(task["id"]))
        start = time.time()
        self.assertTrue(self.eng.request_stop(task["id"]))
        child.wait(30)
        self.assertLess(time.time() - start, 15)
        done = self.eng.store.load(task["id"])
        self.assertEqual(done["status"], "interrupted")
        self.assertEqual(done["steps"][0]["status"], "pending", "the step continues on resume")
        self.assertTrue(done["projects"]["admin-ui"]["after"]["git"], "Foreman still recorded what changed")
        self.assertFalse(self.eng.is_running(task["id"]))
        self.assertFalse(os.path.exists(self.eng.store.path(task["id"], "stop")))
        stops = [e for e in self.eng.store.events(task["id"]) if e["event"] == "stop_requested"]
        self.assertEqual(len(stops), 1, "logged once, by the run itself")
        self.assertEqual(stops[0]["actor"], "cli")


if __name__ == "__main__":
    unittest.main()
