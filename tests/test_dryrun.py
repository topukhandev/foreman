"""Dry run, JSON output and doctor --fix."""

import contextlib
import io
import json
import unittest
from unittest import mock

from helpers import Script, Workspace, make_engine
from test_scenarios import editing_worker, plan, step

from orchestrator import cli
from orchestrator.report import task_summary


class DryRunTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.s = Script()
        self.s.plan = lambda req: plan([("orders-api", "modify")], [step("1", "orders-api", text="add currency")],
                                       checks=[{"project": "orders-api", "command": "npm test", "why": "regression"}])
        self.s.step = editing_worker()
        self.eng = make_engine(self.ws, self.s)

    def tearDown(self):
        self.ws.cleanup()

    def test_dry_run_plans_but_changes_nothing(self):
        res = self.eng.handle("Add a currency to orders", self.ws.api, dry_run=True)
        task = res["task"]
        self.assertEqual(task["status"], "planned")
        self.assertIn("plan only", res["report"])
        self.assertIn("add currency", res["report"])
        self.assertIn("npm test", res["report"])
        self.assertEqual([l for l in self.s.log if ":step:" in l or ":verify" in l], [], "no worker, no QA")
        self.assertEqual(task["steps"][0]["status"], "pending")
        self.assertFalse([e for e in task["evidence"] if e["kind"] == "check"], "no check ran")
        self.assertEqual(self.eng.store.all()[0]["id"], task["id"])

    def test_resume_runs_the_dry_run_plan_without_planning_again(self):
        task = self.eng.handle("Add a currency to orders", self.ws.api, dry_run=True)["task"]
        res = self.eng.resume(task["id"], cwd=self.ws.api)
        self.assertEqual(res["task"]["status"], "done")
        self.assertEqual([l for l in self.s.log if l.endswith(":plan")], ["claude:plan"], "planned once")
        self.assertTrue(any(l == "claude:step:orders-api" for l in self.s.log))

    def test_dry_run_with_open_question_waits_for_the_answer(self):
        self.s.plan = lambda req: plan([("orders-api", "modify")], [step("1", "orders-api")],
                                       questions=[{"kind": "business", "text": "Which currency?", "options": ["USD", "EUR"]}])
        res = self.eng.handle("Add a currency to orders", self.ws.api, dry_run=True)
        self.assertEqual(res["task"]["status"], "needs_input")
        self.assertIn("Which currency?", res["report"])
        self.assertNotIn("foreman resume", res["report"])

    def test_instant_answers_are_unaffected(self):
        res = self.eng.handle("what's the state of my task?", self.ws.api, dry_run=True)
        self.assertIsNone(res["task"])


class JsonTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.s = Script()
        self.s.plan = lambda req: plan([("orders-api", "modify")], [step("1", "orders-api")])
        self.s.step = editing_worker()
        self.eng = make_engine(self.ws, self.s)

    def tearDown(self):
        self.ws.cleanup()

    def test_task_summary_is_json_serialisable_and_compact(self):
        task = self.eng.handle("Add a currency", self.ws.api)["task"]
        data = json.loads(json.dumps(task_summary(task)))
        self.assertEqual(data["status"], "done")
        self.assertEqual(data["projects"]["orders-api"], "modify")
        self.assertEqual(data["steps"][0]["project"], "orders-api")
        self.assertEqual(data["checks"], [{"project": "orders-api", "command": "npm test", "ok": True}])

    def test_print_result_json(self):
        res = self.eng.handle("Add a currency", self.ws.api, dry_run=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli._print_result(self.eng, res, as_json=True)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["task"]["status"], "planned")
        self.assertTrue(data["task"]["dry_run"])
        self.assertIn("report", data)

    def test_lifecycle_data(self):
        data = self.eng.lifecycle_data(self.ws.api)
        self.assertEqual(data["project"], "orders-api")
        self.assertIn("development", data["modes"])
        self.assertEqual(self.eng.lifecycle_data("/")["project"], None)


class DoctorFixTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.eng = make_engine(self.ws, Script())

    def tearDown(self):
        self.ws.cleanup()

    def test_fix_registers_only_where_missing(self):
        data = {"mcp": {"claude": "not registered", "antigravity": "registered", "codex": "not installed"},
                "projects": 4, "providers": {"claude": {"available": True}}}
        with mock.patch("orchestrator.integrations.install", return_value={"claude": "ok"}) as install:
            actions = cli.doctor_fix(self.eng, data)
        install.assert_called_once_with(["claude"])
        self.assertEqual(actions, ["mcp install claude: ok"])

    def test_fix_does_nothing_when_healthy(self):
        data = {"mcp": {"claude": "registered"}, "projects": 4, "providers": {"claude": {"available": True}}}
        with mock.patch("orchestrator.integrations.install") as install:
            self.assertEqual(cli.doctor_fix(self.eng, data), [])
        install.assert_not_called()

    def test_fix_reports_missing_cli_and_rescans_projects(self):
        data = {"mcp": {}, "projects": 0, "providers": {"claude": {"available": False}}}
        actions = cli.doctor_fix(self.eng, data)
        self.assertTrue(actions[0].startswith("scanned"))
        self.assertIn("no AI CLI found", actions[1])

    def test_doctor_data_is_json_serialisable(self):
        with mock.patch("orchestrator.integrations.status", return_value={"claude": "registered"}):
            data = cli.doctor_data(self.eng)
        json.dumps(data)
        self.assertEqual(data["projects"], 4)
        self.assertIn("claude", data["providers"])


if __name__ == "__main__":
    unittest.main()
