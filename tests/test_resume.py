"""A task that stops part-way (a worker's question, a blocked or failed step) keeps its plan: the steps after it wait
instead of being skipped, nothing is "fixed" from a half-done plan, the user sees every question (with the worker's
options and recommendation), and continuing picks up exactly where it stopped, in the same sessions. Replays the
failure seen in task #26: a technical question that was dropped, eight skipped steps and a task stuck as blocked."""

import unittest

from helpers import Script, Workspace, make_engine
from test_lifecycle import with_mode
from test_scenarios import editing_worker, plan, step

from orchestrator.engine import _instructions, _split_answers
from orchestrator.report import render_report


def chain():
    """s0 -> s1 -> s2 -> s3 in orders-api (admin-ui, its consumer, is inspected so no extra impact step is added)."""
    return with_mode(plan([("orders-api", "modify"), ("admin-ui", "inspect")],
                [step("s0", "orders-api"), step("s1", "orders-api", deps=["s0"]), step("s2", "orders-api", deps=["s1"]),
                 step("s3", "orders-api", deps=["s2"])],
                checks=[{"project": "orders-api", "command": "node test.js"}]), "development", feature="dataset")


class ResumeTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.s = Script()
        self.s.plan = lambda req: chain()
        self.eng = make_engine(self.ws, self.s)
        self.work = editing_worker()

    def tearDown(self):
        self.ws.cleanup()

    def worker(self, ask_in="s1", asks=None, status="needs_input", summary="Checkpoint 1: two deviations need a decision"):
        """Edits files; the step `ask_in` returns once with `asks` (or the given status) instead of finishing."""
        state = {"asked": False}

        def run(req):
            data = self.work(req)
            if f".{ask_in}" not in self._step_of(req) or state["asked"]:
                return data
            state["asked"] = True
            return {"status": status, "summary": summary, "questions": asks or []}
        self.s.step = run
        return state

    def _step_of(self, req):
        task = self.eng.store.load("1")
        running = [s for s in task["steps"] if s["status"] == "running"]
        return running[0]["id"] if running else ""

    def kinds(self):
        return [e["event"] for e in self.eng.store.events("1")]

    # ------------------------------------------------------------------ questions
    def test_a_workers_technical_question_reaches_the_user_with_its_recommendation(self):
        self.worker(asks=[
            {"kind": "technical", "text": "No Parquet reader is installed. Install pyarrow or drop Wikidata aliases?",
             "options": ["Drop Wikidata aliases", "Install pyarrow"], "default": "Install pyarrow"},
            {"kind": "technical", "text": "Exempt Bangladesh rows from the 10% per-category cap?",
             "options": ["Yes", "No"], "default": "No"}])
        task = self.eng.handle("build the dataset", self.ws.api)["task"]
        self.assertEqual(task["status"], "needs_input")
        q1, q2 = task["questions"]
        self.assertEqual(q1["options"], ["Install pyarrow", "Drop Wikidata aliases"], "recommendation first")
        self.assertEqual((q1["kind"], q1["from_step"]), ("technical", "r1.0.s1"))
        by = {s["id"]: s["status"] for s in task["steps"]}
        self.assertEqual(by, {"r1.0.s0": "done", "r1.0.s1": "needs_input", "r1.0.s2": "pending", "r1.0.s3": "pending"})
        self.assertNotIn("check", self.kinds(), "a half-done plan is not checked or fixed")
        self.assertFalse(any(s["id"].startswith("fix") for s in task["steps"]))
        report = render_report(task)
        self.assertIn("Needs your decision", report)
        self.assertIn("Install pyarrow (recommended)", report)
        self.assertIsNone(task.get("next"), "no misleading next step while a question is open")

        plans = self.s.log.count("claude:plan")
        claude = self.eng.providers["claude"]
        before = len(claude.calls)
        task = self.eng.answer("1", "1. Install pyarrow\n2. No")["task"]
        self.assertEqual(task["status"], "done")
        self.assertEqual(self.s.log.count("claude:plan"), plans, "answers to a worker don't trigger re-planning")
        self.assertEqual([q["answer"] for q in task["questions"]], ["Install pyarrow", "No"])
        resumed = claude.calls[before]
        self.assertEqual(resumed.session_id, task["steps"][1]["history"][0]["session"], "back in the asking session")
        self.assertIn("A: Install pyarrow", resumed.prompt)
        self.assertEqual([s["status"] for s in task["steps"]], ["done"] * 4)
        self.assertEqual(task["next"]["mode"], "qa", "a finished development task moves on to QA")

    def test_a_worker_that_stops_for_input_without_asking_still_gets_a_question(self):
        self.worker(asks=[])
        task = self.eng.handle("build the dataset", self.ws.api)["task"]
        self.assertEqual(task["status"], "needs_input")
        self.assertEqual(len(task["questions"]), 1)
        self.assertIn("Checkpoint 1: two deviations need a decision", task["questions"][0]["text"])

    def test_a_blocked_worker_asks_and_skipping_its_step_lets_the_rest_run(self):
        self.worker(status="blocked", summary="The vendor API needs a key I don't have")
        task = self.eng.handle("build the dataset", self.ws.api)["task"]
        q = task["questions"][0]
        self.assertEqual((q["kind"], q["options"]), ("blocker", ["Try again", "Skip this step"]))
        task = self.eng.answer("1", "Skip this step")["task"]
        self.assertEqual(task["status"], "done")
        by = {s["id"]: s for s in task["steps"]}
        self.assertTrue(by["r1.0.s1"]["skipped_by_user"])
        self.assertEqual(by["r1.0.s3"]["status"], "done")

    # ------------------------------------------------------------------ failed steps and "continue"
    def test_a_failed_step_keeps_the_plan_and_continue_carries_on(self):
        fail = {"on": True}
        self.s.fail_steps = lambda provider, req: fail["on"] and "s1" in self._step_of(req)
        self.s.step = self.work
        task = self.eng.handle("build the dataset", self.ws.api)["task"]
        self.assertEqual(task["status"], "blocked")
        self.assertEqual([s["status"] for s in task["steps"]], ["done", "failed", "pending", "pending"])
        self.assertNotIn("check", self.kinds())
        self.assertTrue(any("waiting for r1.0.s1" in r for r in task["remaining"]))
        self.assertEqual(task["next"]["resume"], "1")
        self.assertEqual(task["next"]["title"], "Continue #1")
        self.assertIn("3 steps left, starting with r1.0.s1", task["next"]["reason"])
        report = render_report(task, chat=True)
        self.assertIn("Stopped at step r1.0.s1 in orders-api", report)
        self.assertIn("2 more steps after it", report)
        self.assertIn("Say \"continue\"", report)
        self.assertNotIn("Next step", report, "no \"start development\" for an unfinished task")
        status = self.eng.status("1", self.ws.api)["report"]
        self.assertIn("✘ r1.0.s1 orders-api implement", status)
        self.assertIn("· r1.0.s2 orders-api implement", status)

        fail["on"] = False
        plans = self.s.log.count("claude:plan")
        nxt, msg = self.eng.route("next", self.ws.api)
        self.assertEqual(nxt["id"], "1", "\"next\" continues the unfinished task instead of opening a new one")
        task = self.eng.execute(nxt)["task"]
        self.assertEqual(task["status"], "done")
        self.assertEqual(self.s.log.count("claude:plan"), plans, "the existing plan is run, not re-planned")
        self.assertEqual([s["status"] for s in task["steps"]], ["done"] * 4)
        self.assertEqual(len(self.eng.store.all()), 1)

    def test_a_task_finished_another_way_is_closed_and_never_continued(self):
        self.worker(asks=[{"kind": "technical", "text": "Install pyarrow?", "options": ["Yes", "No"], "default": "Yes"}])
        task = self.eng.handle("build the dataset", self.ws.api)["task"]
        self.assertEqual(task["status"], "needs_input")
        task, msg = self.eng.route("close #1: the fix round already built everything", self.ws.api)
        self.assertIsNone(task)
        self.assertIn("Closed by you", msg)
        self.assertIn("the fix round already built everything", msg)
        task = self.eng.store.load("1")
        self.assertEqual((task["status"], task["remaining"]), ("done", []))
        self.assertTrue(all(s["status"] == "done" or s.get("skipped_by_user") for s in task["steps"]))
        self.assertEqual(task["questions"][0]["answer"], "(closed by you)")
        self.assertEqual(task["next"]["mode"], "qa", "closed as finished: QA comes next")
        nxt, msg = self.eng.route("continue", self.ws.api)
        self.assertNotEqual((nxt or {}).get("status"), "queued", "a closed task is not continued")
        self.assertIn("already done", self.eng.close("1")["report"])
        self.assertEqual(self.eng.close("1", cancel=True)["task"]["status"], "done")

    def test_cancel_drops_a_task(self):
        self.worker(status="blocked", summary="no key")
        self.eng.handle("build the dataset", self.ws.api)
        task, msg = self.eng.route("cancel #1", self.ws.api)
        self.assertIn("Cancelled by you", msg)
        task = self.eng.store.load("1")
        self.assertEqual((task["status"], task.get("next")), ("cancelled", None))

    def test_continue_with_new_instructions_re_plans(self):
        self.s.fail_steps = lambda provider, req: "s1" in self._step_of(req)
        self.s.step = self.work
        self.eng.handle("build the dataset", self.ws.api)
        self.s.fail_steps = False
        plans = self.s.log.count("claude:plan")
        self.eng.handle("continue, but use the CSV dumps instead of Parquet", self.ws.api)
        self.assertEqual(self.s.log.count("claude:plan"), plans + 1)

    def test_what_counts_as_new_instructions_and_numbered_answers(self):
        self.assertEqual(_instructions("continue"), "")
        self.assertEqual(_instructions("resume #26"), "")
        self.assertEqual(_instructions("continue the previous task please"), "")
        self.assertEqual(_instructions("continue where you left off"), "")
        self.assertTrue(_instructions("continue, but use the CSV dumps"))
        self.assertEqual(_split_answers("1. pyarrow\n2) no\n   (keep the cap)", 2), ["pyarrow", "no (keep the cap)"])
        self.assertEqual(_split_answers("use the defaults", 2), ["use the defaults"] * 2)
        self.assertEqual(_split_answers("1. only the first", 2), ["only the first", "1. only the first"])

    def test_a_fix_round_that_succeeds_ends_done(self):
        self.s.step = self.work
        bad = {"left": 1}  # the first checks fail, the fix round repairs it
        orig = self.eng._run_checks

        def checks(task, changed):
            res = orig(task, changed)
            if bad["left"]:
                bad["left"] -= 1
                return [{**c, "ok": False, "tail": "boom"} for c in res]
            return res
        self.eng._run_checks = checks
        task = self.eng.handle("build the dataset", self.ws.api)["task"]
        self.assertEqual((task["status"], task["fix_rounds"]), ("done", 1))
        self.assertEqual(task["remaining"], [])
        self.assertEqual(task["next"]["mode"], "qa")


if __name__ == "__main__":
    unittest.main()
