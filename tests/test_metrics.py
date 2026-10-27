"""Execution metrics: usage reported by each CLI is parsed, journaled per attempt and per planner call, summed per
task, and totalled by `foreman stats` (old tasks without metrics still count)."""

import contextlib
import io
import json
import os
import unittest

from helpers import Script, Workspace, make_engine, write

from orchestrator.config import ProviderSettings
from orchestrator.metrics import render_stats, stats, task_metrics
from orchestrator.providers.antigravity import AntigravityProvider
from orchestrator.providers.base import RunRequest
from orchestrator.providers.claude import ClaudeProvider
from orchestrator.providers.opencode import OpenCodeProvider


def fake_exec(out):
    return lambda cmd, req: (0, out, "", 2.5, False)


class AdapterUsageTest(unittest.TestCase):
    def test_claude_result_line(self):
        p = ClaudeProvider(ProviderSettings(name="claude", command="claude"))
        out = json.dumps({"type": "result", "is_error": False, "result": "ok", "session_id": "s1",
                          "total_cost_usd": 0.42, "usage": {"input_tokens": 12, "output_tokens": 300,
                                                            "cache_read_input_tokens": 1000,
                                                            "cache_creation_input_tokens": 50}})
        p._exec = fake_exec(out)
        res = p.run(RunRequest(prompt="x", cwd="/tmp"))
        self.assertTrue(res.ok)
        self.assertEqual(res.cost_usd, 0.42)
        self.assertEqual(res.tokens, {"input": 12, "output": 300, "cache_read": 1000, "cache_write": 50})

    def test_antigravity_result_payload(self):
        p = AntigravityProvider(ProviderSettings(name="antigravity", command="agy"))
        out = json.dumps({"event": "result", "result": {"conversation_id": "c1", "status": "SUCCESS", "response": "ok",
                                                         "usage": {"input_tokens": 5, "output_tokens": 7,
                                                                   "thinking_tokens": 3, "cache_read_tokens": 9}}})
        p._exec = fake_exec(out)
        res = p.run(RunRequest(prompt="x", cwd="/tmp"))
        self.assertTrue(res.ok)
        self.assertIsNone(res.cost_usd)  # agy doesn't report cost: left out, never estimated
        self.assertEqual(res.tokens, {"input": 5, "output": 7, "reasoning": 3, "cache_read": 9})

    def test_opencode_sums_step_finish_events(self):
        p = OpenCodeProvider(ProviderSettings(name="opencode", command="opencode"))
        fin = {"type": "step_finish", "sessionID": "o1",
               "part": {"type": "step-finish", "cost": 0.01,
                        "tokens": {"input": 10, "output": 20, "reasoning": 1, "cache": {"read": 100, "write": 0}}}}
        out = "\n".join([json.dumps({"type": "text", "sessionID": "o1", "part": {"type": "text", "text": "ok"}}),
                         json.dumps(fin), json.dumps(fin)])
        p._exec = fake_exec(out)
        res = p.run(RunRequest(prompt="x", cwd="/tmp"))
        self.assertTrue(res.ok)
        self.assertAlmostEqual(res.cost_usd, 0.02)
        self.assertEqual(res.tokens, {"input": 20, "output": 40, "reasoning": 2, "cache_read": 200})


class EngineMetricsTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.s = Script()
        self.eng = make_engine(self.ws, self.s)
        for prov in self.eng.providers.values():  # every run reports usage, like Claude does
            inner = prov.handler

            def priced(req, _inner=inner):
                res = _inner(req)
                res.cost_usd, res.tokens = 0.1, {"input": 10, "output": 5}
                return res
            prov.handler = priced

    def tearDown(self):
        self.ws.cleanup()

    def test_attempts_and_planner_calls_are_measured(self):
        self.s.plan = lambda req: {"intent": "change", "title": "t", "projects": [{"name": "admin-ui", "role": "modify",
                                                                                   "why": "x"}],
                                   "steps": [{"id": "1", "project": "admin-ui", "kind": "implement",
                                              "instructions": "edit"}], "questions": []}

        def work(req):
            write(req.cwd, "src/x.js", "// x\n")
            return {"status": "done", "summary": "edited"}
        self.s.step = work
        task = self.eng.handle("change the ui", self.ws.ui)["task"]
        attempt = task["steps"][0]["history"][0]
        self.assertEqual(attempt["cost_usd"], 0.1)
        self.assertEqual(attempt["tokens"], {"input": 10, "output": 5})
        self.assertIn("duration", attempt)
        events = self.eng.store.events(task["id"])
        purposes = [e["purpose"] for e in events if e["event"] == "thinking_done"]
        self.assertEqual(purposes, ["plan"])
        m = task["metrics"]
        self.assertEqual(m["calls"], 3)  # plan + step + QA verify step
        self.assertEqual(m["worker_calls"], 2)
        self.assertAlmostEqual(m["cost_usd"], 0.3)
        self.assertEqual(m["tokens"], {"input": 30, "output": 15})
        self.assertEqual(m["checks"], 1)
        self.assertGreaterEqual(m["active_sec"], 0)
        # Persisted with the task.
        self.assertEqual(self.eng.store.load(task["id"])["metrics"]["calls"], 3)

    def test_handoffs_retries_and_failures_are_counted(self):
        task = {"steps": [{"history": [{"provider": "claude", "outcome": "limit", "duration": 3},
                                       {"provider": "opencode", "outcome": "done", "duration": 4}]}],
                "evidence": [{"kind": "check", "ok": False}], "questions": [{"kind": "business"}], "fix_rounds": 1}
        events = [{"event": "run_started", "at": "2026-10-27T10:00:00"}, {"event": "step_retry"},
                  {"event": "finished", "at": "2026-10-27T10:05:00"}]
        m = task_metrics(task, events)
        self.assertEqual((m["calls"], m["failed_calls"], m["handoffs"], m["retries"]), (2, 1, 1, 1))
        self.assertEqual((m["checks"], m["checks_failed"], m["fix_rounds"]), (1, 1, 1))
        self.assertEqual(m["questions"], {"business": 1})
        self.assertEqual(m["active_sec"], 300)
        self.assertEqual(m["busy_sec"], 7)
        self.assertNotIn("cost_usd", m)  # nothing reported a cost


class StatsTest(unittest.TestCase):
    def test_totals_and_old_tasks_without_metrics(self):
        tasks = [
            {"status": "done", "created_at": "2026-10-27T09:00:00",
             "metrics": {"calls": 4, "retries": 1, "handoffs": 0, "fix_rounds": 1, "checks": 2, "checks_failed": 1,
                         "busy_sec": 60, "active_sec": 120, "failed_calls": 0, "questions": {}, "cost_usd": 1.5}},
            {"status": "partial", "created_at": "2026-10-27T10:00:00",
             "metrics": {"calls": 2, "retries": 0, "handoffs": 1, "fix_rounds": 0, "checks": 1, "checks_failed": 0,
                         "busy_sec": 30, "active_sec": 60, "failed_calls": 1, "questions": {"business": 1}}},
            {"status": "answered", "created_at": "2026-10-01T10:00:00"},  # recorded before metrics existed
        ]
        s = stats(tasks)
        self.assertEqual((s["tasks"], s["measured"]), (3, 2))
        self.assertEqual(s["completion_rate"], 0.67)
        self.assertEqual(s["totals"]["calls"], 6)
        self.assertEqual(s["per_task"]["calls"], 3)
        self.assertEqual(s["cost_usd"], 1.5)
        self.assertEqual(s["priced_tasks"], 1)
        self.assertEqual(s["tasks_with_questions"], 1)
        self.assertEqual(stats(tasks, since="2026-10-27")["tasks"], 2)
        text = render_stats(s)
        self.assertIn("3 tasks (2 with metrics)", text)
        self.assertIn("$1.5 reported by providers", text)
        self.assertEqual(render_stats(stats([])), "No tasks recorded yet.")


class StatsCommandTest(unittest.TestCase):
    def test_cli_stats_runs_without_ai(self):
        ws = Workspace()
        try:
            from orchestrator.cli import main
            old = os.environ.get("FOREMAN_HOME")
            os.environ["FOREMAN_HOME"] = ws.home
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    code = main(["stats", "--json"])
            finally:
                if old is None:
                    os.environ.pop("FOREMAN_HOME", None)
                else:
                    os.environ["FOREMAN_HOME"] = old
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(buf.getvalue())["tasks"], 0)
        finally:
            ws.cleanup()


if __name__ == "__main__":
    unittest.main()
