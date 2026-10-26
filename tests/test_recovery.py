"""What happens when a worker's run is cut off: usage limits, network errors, crashes, missing reports, a full
context, every provider out of limits, and Foreman itself dying mid-step. The work must carry on from where it
stopped, in the same session when possible, otherwise in another provider's session with a handoff brief."""

import os
import time
import unittest

from helpers import Script, Workspace, make_engine, write
from test_scenarios import plan, step

from orchestrator import engine as engine_mod
from orchestrator.brain import PLAN_SCHEMA, STEP_SCHEMA
from orchestrator.providers.base import (CONTEXT, CRASH, LIMIT, OUTPUT, TRANSIENT, RunResult, classify,
                                         reset_time)
from orchestrator.providers.mock import MockProvider


class Scripted:
    """Per-provider list of outcomes for worker steps: a RunResult to return, or None to work normally."""

    def __init__(self, test, name, outcomes, session=None, before=None):
        self.inner = test.s.handler(name)
        self.outcomes = list(outcomes)
        self.session = session
        self.before = before
        self.requests = []

    def __call__(self, req):
        if req.schema is not STEP_SCHEMA:
            return self.inner(req)
        self.requests.append(req)
        if self.session and req.on_session:
            req.on_session(req.session_id or self.session)
        if self.before:
            self.before(req)
        outcome = self.outcomes.pop(0) if self.outcomes else None
        return outcome if outcome is not None else self.inner(req)


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.s = Script()
        self.s.plan = lambda req: plan([("orders-api", "inspect")], [step("1", "orders-api", kind="investigate")])
        self.eng = make_engine(self.ws, self.s)
        self.old_waits = engine_mod.TRANSIENT_WAITS
        engine_mod.TRANSIENT_WAITS = (0.01, 0.01, 0.01)

    def tearDown(self):
        engine_mod.TRANSIENT_WAITS = self.old_waits
        self.ws.cleanup()

    def script(self, name, outcomes, session=None, before=None) -> Scripted:
        h = Scripted(self, name, outcomes, session, before)
        self.eng.providers[name] = MockProvider(name=name, handler=h)
        return h

    def events(self, task_id="1"):
        return self.eng.store.events(task_id)

    # ------------------------------------------------------------------ classification
    def test_failure_kinds_and_reset_times_are_read_from_provider_messages(self):
        self.assertEqual(classify("You've hit your session limit · resets 7:10pm (Asia/Dhaka)"), LIMIT)
        self.assertEqual(classify("API Error: 529 overloaded"), TRANSIENT)
        self.assertEqual(classify("API Error: Connection error."), TRANSIENT)
        self.assertEqual(classify("Prompt is too long"), CONTEXT)
        self.assertEqual(classify("exit 1"), CRASH)
        now = time.mktime((2026, 10, 26, 18, 0, 0, 0, 0, -1))
        at = time.localtime(reset_time("You've hit your session limit · resets 7:10pm", now))
        self.assertEqual((at.tm_hour, at.tm_min, at.tm_mday), (19, 10, 26))
        at = time.localtime(reset_time("resets 5am", now))
        self.assertEqual((at.tm_hour, at.tm_mday), (5, 27), "a time already past today means tomorrow")
        self.assertEqual(reset_time("please try again in 15 minutes", now), now + 900)
        self.assertIsNone(reset_time("You've hit your limit", now))
        self.assertIsNotNone(reset_time("resets 7:10pm (Asia/Dhaka)", now))

    # ------------------------------------------------------------------ usage limits
    def test_a_usage_limit_hands_the_step_to_another_provider_with_a_handoff(self):
        claude = self.script("claude", [RunResult(False, error="You've hit your session limit · resets 7:10pm")],
                             session="c-1")
        opencode = self.script("opencode", [])
        task = self.eng.handle("look into currency", self.ws.api)["task"]
        self.assertEqual(task["status"], "done")
        self.assertEqual(len(claude.requests), 1, "no pointless fresh retry of a provider out of its limit")
        take = opencode.requests[0]
        self.assertIn("HANDOFF", take.prompt)
        self.assertIn("cut off by the provider's usage limit", take.prompt)
        self.assertIn("claude", self.eng.unhealthy(), "rested until its reset")
        self.assertIn("resets", self.eng.unhealthy()["claude"])
        hist = task["steps"][0]["history"]
        self.assertEqual([(a["provider"], a["outcome"]) for a in hist], [("claude", LIMIT), ("opencode", "done")])
        self.assertTrue(any("usage limit" in n and "takes over" in n for n in task["notices"]))
        # the next task's planning skips claude while it rests
        before = len(self.s.log)
        self.eng.handle("look into totals", self.ws.api)
        self.assertNotIn("claude:plan", self.s.log[before:])

    def test_when_every_provider_is_out_of_limits_foreman_waits_for_the_stated_reset(self):
        soon = time.time() + 0.4
        self.script("claude", [RunResult(False, error="limit", kind=LIMIT, retry_at=soon)], session="c-1")
        self.script("opencode", [RunResult(False, error="Rate limit reached", kind=LIMIT, retry_at=soon + 60)])
        task = self.eng.handle("look into currency", self.ws.api)["task"]
        self.assertEqual(task["status"], "done")
        waiting = [e for e in self.events() if e["event"] == "waiting"]
        self.assertTrue(waiting and "claude" in waiting[0]["reason"])
        hist = task["steps"][0]["history"]
        self.assertEqual(hist[-1]["provider"], "claude", "claude's limit reset first")
        self.assertEqual(hist[-1]["session"], "c-1", "and it resumed its own session")
        self.assertNotIn("waiting", task)

    def test_without_a_reset_time_it_checks_periodically_then_pauses(self):
        self.eng.cfg.limits.limit_retry_sec = 0.05
        self.eng.cfg.limits.limit_retries = 3
        lim = RunResult(False, error="quota exceeded", kind=LIMIT)
        self.script("claude", [lim] * 20, session="c-1")
        self.script("opencode", [lim] * 20)
        task = self.eng.handle("look into currency", self.ws.api)["task"]
        self.assertEqual(task["status"], "paused")
        self.assertEqual(task["steps"][0]["status"], "paused")
        checks = [e["check"] for e in self.events() if e["event"] == "waiting"]
        self.assertEqual(max(checks), 3)
        self.assertTrue(any(n.startswith("Paused:") for n in task["notices"]))
        from orchestrator.report import render_report
        report = render_report(task, chat=True)
        self.assertTrue(report.startswith("‖"))
        self.assertIn("Paused at step r1.0.1 in orders-api", report)
        self.assertIn("out of their usage limits", report)
        # "continue" later picks it up in claude's session, with a fresh allowance of checks
        self.script("claude", [], session="c-1")
        self.eng._provider_failed("claude", "", "")
        os.remove(os.path.join(self.eng.cfg.home, "health.json"))
        task = self.eng.resume(task["id"], cwd=self.ws.api)["task"]
        self.assertEqual(task["status"], "done")

    def test_a_known_reset_too_far_away_pauses_at_once(self):
        far = time.time() + 3 * 86400
        self.script("claude", [RunResult(False, error="weekly limit", kind=LIMIT, retry_at=far)] * 5, session="c-1")
        self.script("opencode", [RunResult(False, error="weekly limit", kind=LIMIT, retry_at=far)] * 5)
        started = time.time()
        task = self.eng.handle("look into currency", self.ws.api)["task"]
        self.assertLess(time.time() - started, 5)
        self.assertEqual(task["status"], "paused")
        self.assertIn("out of their usage limits", task["steps"][0]["summary"])

    def test_planning_waits_for_a_limit_reset_instead_of_failing(self):
        soon = time.time() + 0.3
        calls = []
        inner = self.s.handler("claude")

        def claude(req):
            if req.schema is PLAN_SCHEMA and not calls:
                calls.append(1)
                return RunResult(False, error="session limit", kind=LIMIT, retry_at=soon)
            return inner(req)
        self.eng.providers["claude"] = MockProvider(name="claude", handler=claude)
        self.eng.providers.pop("opencode")
        task = self.eng.handle("look into currency", self.ws.api)["task"]
        self.assertEqual(task["status"], "done")
        self.assertIn("waiting", [e["event"] for e in self.events()])

    def test_a_provider_near_its_limit_gets_no_new_work_if_another_can_do_it(self):
        self.eng._note_usage("claude", {"utilization": 0.95, "resets_at": time.time() + 3600})
        claude = self.script("claude", [])
        opencode = self.script("opencode", [])
        self.eng.handle("look into currency", self.ws.api)
        self.assertEqual((len(claude.requests), len(opencode.requests)), (0, 1))
        self.assertIn("opencode:plan", self.s.log, "planning moves too")

    # ------------------------------------------------------------------ same-session recovery
    def test_a_network_error_resumes_the_same_session(self):
        claude = self.script("claude", [RunResult(False, error="API Error: Connection error.")], session="c-1")
        task = self.eng.handle("look into currency", self.ws.api)["task"]
        self.assertEqual(task["status"], "done")
        second = claude.requests[1]
        self.assertEqual((second.session_id, second.fork), ("c-1", False))
        self.assertIn("cut off by a network or API error", second.prompt)
        self.assertNotIn("YOUR STEP", second.prompt, "the session already has the instructions")
        self.assertIn("step_retry", [e["event"] for e in self.events()])

    def test_a_missing_final_report_is_asked_for_in_the_same_session(self):
        claude = self.script("claude", [RunResult(False, error="no structured output", kind=OUTPUT, session_id="c-1")])
        task = self.eng.handle("look into currency", self.ws.api)["task"]
        self.assertEqual(task["status"], "done")
        second = claude.requests[1]
        self.assertEqual(second.session_id, "c-1")
        self.assertIn("Do not redo the work", second.prompt)

    def test_a_full_context_continues_in_a_fresh_session_with_a_handoff(self):
        claude = self.script("claude", [RunResult(False, error="Prompt is too long")], session="c-1")
        task = self.eng.handle("look into currency", self.ws.api)["task"]
        self.assertEqual(task["status"], "done")
        second = claude.requests[1]
        self.assertIsNone(second.session_id)
        self.assertIn("HANDOFF", second.prompt)
        self.assertIn("cut off by a full context window", second.prompt)

    # ------------------------------------------------------------------ Foreman dies mid-step
    def test_the_session_id_is_saved_while_the_worker_is_still_running(self):
        seen = []

        def check(req):
            saved = self.eng.store.load("1")["steps"][0]
            seen.append((saved["session_id"], saved["provider"], saved["history"][-1]["session"]))
        self.script("claude", [], session="live-1", before=check)
        self.eng.handle("look into currency", self.ws.api)
        self.assertEqual(seen, [("live-1", "claude", "live-1")])

    def test_after_foreman_was_killed_continue_resumes_the_cut_off_session(self):
        self.eng.registry.ensure_scanned(self.ws.api)
        task = self.eng.prepare_new("look into currency", self.ws.api)
        self.eng._plan(task)
        s = task["steps"][0]
        # what a killed process leaves behind: a running step, an attempt that never ended, a live session id
        s.update(status="running", provider="claude", session_id="c-9",
                 history=[{"provider": "claude", "session": "c-9", "started": "2026-10-26T10:00:00"}])
        task["status"], task["runs"] = "running", 1
        self.eng.store.save(task)
        claude = self.script("claude", [])
        task = self.eng.resume(task["id"], cwd=self.ws.api)["task"]
        self.assertEqual(task["status"], "done")
        req = claude.requests[0]
        self.assertEqual(req.session_id, "c-9")
        self.assertIn("cut off by Foreman being stopped or closed", req.prompt)
        self.assertEqual(task["steps"][0]["history"][0]["outcome"], "interrupted")

    # ------------------------------------------------------------------ handoff contents
    def test_the_handoff_lists_changed_files_progress_notes_and_last_actions(self):
        self.s.plan = lambda req: plan([("orders-api", "modify"), ("admin-ui", "inspect")], [step("1", "orders-api")])

        def half_done(req):
            if req.on_activity:
                req.on_activity("Write(data/part1.json)")
            write(self.ws.api, "data/part1.json", "{}")
            notes = [line for line in req.prompt.splitlines() if line.startswith("PROGRESS NOTES")]
            path = notes[0].split("append one line to ")[1].split(" (")[0]
            with open(path, "a") as f:
                f.write("part 1 downloaded; next: part 2\n")
        self.script("claude", [RunResult(False, error="You've hit your session limit")], session="c-1",
                    before=half_done)
        self.eng.providers["claude"].capabilities.writes_extra_dirs = True
        opencode = self.script("opencode", [])
        self.eng.handle("download the data", self.ws.api)
        take = opencode.requests[0].prompt
        self.assertIn("data/part1.json", take)
        self.assertIn("part 1 downloaded; next: part 2", take)
        self.assertIn("Write(data/part1.json)", take)
        self.assertNotIn("PROGRESS NOTES", take, "opencode can't write outside the project, so it isn't asked to")


if __name__ == "__main__":
    unittest.main()
