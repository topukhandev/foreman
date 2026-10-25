"""A worker that runs out of time resumes the same session with more time, instead of starting over."""

import unittest

from helpers import Script, Workspace, make_engine
from test_scenarios import plan, step

from orchestrator.brain import PLAN_SCHEMA, STEP_SCHEMA
from orchestrator.providers.base import RunResult, stream_session_id
from orchestrator.providers.mock import MockProvider


class TimeoutResumeTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.s = Script()
        # one read-only step keeps the engine's impact-check step (for left-out consumers) out of the picture
        self.s.plan = lambda req: plan([("orders-api", "inspect")], [step("1", "orders-api", kind="investigate")])
        self.eng = make_engine(self.ws, self.s, providers=["claude"])
        self.eng.cfg.limits.step_timeout_sec = 1200
        self.requests = []

    def tearDown(self):
        self.ws.cleanup()

    def slow_claude(self, timeouts, session="sess-1"):
        """claude times out `timeouts` times on worker steps (reporting its session), then works normally."""
        inner = self.s.handler("claude")
        left = [timeouts]

        def handler(req):
            if req.schema is STEP_SCHEMA:
                self.requests.append(req)
                if left[0] > 0:
                    left[0] -= 1
                    return RunResult(False, error=f"timed out after {req.timeout}s", session_id=session)
            return inner(req)
        self.eng.providers["claude"] = MockProvider(name="claude", handler=handler)

    def test_the_retry_resumes_the_same_session_with_double_the_time_and_a_continue_prompt(self):
        self.slow_claude(1)
        task = self.eng.handle("add currency", self.ws.api)["task"]
        first, second = self.requests[:2]
        self.assertEqual((first.timeout, second.timeout), (1200, 2400))
        self.assertEqual((second.session_id, second.fork), ("sess-1", False))
        self.assertIn("SAME step in this session", second.prompt)
        self.assertNotIn("YOUR STEP", second.prompt, "the instructions are already in the session")
        step_ = task["steps"][0]
        self.assertEqual((step_["status"], step_["timeouts"], step_["session_id"] is not None), ("done", 1, True))
        self.assertTrue(any("continuing the same session" in n for n in task["notices"]))
        events = [e["event"] for e in self.eng.store.events(task["id"])]
        self.assertIn("step_timeout", events)

    def test_time_grows_each_time_and_stops_at_the_ceiling(self):
        self.eng.cfg.limits.step_timeout_max_sec = 4000
        self.slow_claude(3)
        self.eng.handle("add currency", self.ws.api)
        self.assertEqual([r.timeout for r in self.requests[:4]], [1200, 2400, 4000, 4000])

    def test_after_the_allowed_retries_the_step_falls_back_like_any_failure(self):
        self.slow_claude(99)
        task = self.eng.handle("add currency", self.ws.api)["task"]
        step_requests = [r for r in self.requests if r.schema is STEP_SCHEMA]
        self.assertEqual(len(step_requests), 1 + self.eng.cfg.limits.timeout_retries)
        self.assertEqual(task["steps"][0]["status"], "failed")

    def test_without_a_session_to_resume_the_retry_is_fresh_but_told_what_happened(self):
        self.slow_claude(1, session=None)
        self.eng.handle("add currency", self.ws.api)
        second = self.requests[1]
        self.assertIsNone(second.session_id)
        self.assertIn("YOUR STEP", second.prompt)
        self.assertIn("ran out of time", second.prompt)

    def test_the_planner_can_ask_for_more_time_for_a_long_step(self):
        long_step = {**step("1", "orders-api", kind="investigate"), "expected_minutes": 100}
        self.s.plan = lambda req: plan([("orders-api", "inspect")], [long_step])
        self.slow_claude(0)
        task = self.eng.handle("download the dataset", self.ws.api)["task"]
        self.assertEqual(task["steps"][0]["expected_min"], 100)
        self.assertEqual(self.requests[0].timeout, 100 * 90, "about 1.5x what the planner expects")
        self.assertIn("150 minutes", self.requests[0].prompt)
        self.assertIn("expected_minutes", str(PLAN_SCHEMA))

    def test_workers_are_told_the_time_and_to_run_long_jobs_in_the_background(self):
        self.slow_claude(0)
        self.eng.handle("add currency", self.ws.api)
        self.assertIn("about 20 minutes", self.requests[0].prompt)  # 1200 s in this test
        self.assertIn("in the background", self.requests[0].prompt)


class StreamSessionTest(unittest.TestCase):
    def test_session_id_is_read_from_the_first_stream_lines(self):
        out = ('{"type":"system","subtype":"init","session_id":"abc-123","tools":[]}\n'
               '{"type":"assistant","message":{}}\n')
        self.assertEqual(stream_session_id(out), "abc-123")
        self.assertIsNone(stream_session_id("not json\n{}\n"))


if __name__ == "__main__":
    unittest.main()
