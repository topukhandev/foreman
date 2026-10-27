"""Honest outcomes: a malformed result never becomes success, an unavailable review is not approval, projects Foreman
can't see changes in (no git) or can't test are never reported as verified, and the verification level is kept
apart from the task status in every report."""

import json
import os
import unittest

from helpers import Script, Workspace, make_engine, node_pkg, write

from orchestrator.brain import PLAN_SCHEMA, STEP_SCHEMA
from orchestrator.report import render_report, render_status
from orchestrator.util import check_result


def plan(projects, steps):
    return {"intent": "change", "title": "Task", "projects": [{"name": n, "role": r, "why": "x"} for n, r in projects],
            "steps": steps, "questions": []}


def step(project, id_="1"):
    return {"id": id_, "project": project, "kind": "implement", "instructions": "edit", "depends_on": []}


def editing(summary="edited"):
    def run(req):
        write(req.cwd, "src/change.js", "// changed\n")
        return {"status": "done", "summary": summary, "files_changed": ["src/change.js"]}
    return run


class CheckResultTest(unittest.TestCase):
    def test_top_level_problems_are_fatal(self):
        self.assertEqual(check_result({}, STEP_SCHEMA)[0], None)
        self.assertEqual(check_result("text", STEP_SCHEMA), (None, ["the result is not a JSON object"]))
        data, problems = check_result({"status": "finished", "summary": "x"}, STEP_SCHEMA)
        self.assertIsNone(data)
        self.assertIn("'status' is 'finished'", problems[0])
        self.assertIsNone(check_result({"status": "done", "summary": 3}, STEP_SCHEMA)[0])
        self.assertIsNone(check_result({"status": "done", "summary": "x", "findings": "a string"}, STEP_SCHEMA)[0])

    def test_bad_list_items_are_dropped_and_extras_kept(self):
        data, notes = check_result({"status": "done", "summary": "x", "findings": ["ok", 5], "extra": 1,
                                    "questions": [{"text": "q?", "kind": "odd-kind"}, "not an object"]}, STEP_SCHEMA)
        self.assertEqual(data["findings"], ["ok"])
        self.assertEqual(data["questions"], [{"text": "q?", "kind": "odd-kind"}])  # an odd nested enum is tolerated
        self.assertEqual(data["extra"], 1)
        self.assertEqual(len(notes), 2)

    def test_plan_requires_its_fields(self):
        self.assertIsNone(check_result({"intent": "change", "title": "t", "projects": []}, PLAN_SCHEMA)[0])


class HonestOutcomeTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.s = Script()
        self.notes = []
        self.eng = make_engine(self.ws, self.s, notes=self.notes)

    def tearDown(self):
        self.ws.cleanup()

    def test_malformed_final_report_never_becomes_done(self):
        self.s.plan = lambda req: plan([("admin-ui", "modify")], [step("admin-ui")])

        def bad(req):
            write(req.cwd, "src/change.js", "// changed\n")
            return {"status": "done"}  # no summary: not a valid report
        self.s.step = bad
        task = self.eng.handle("change the ui", self.ws.ui)["task"]
        st = task["steps"][0]
        self.assertEqual(st["status"], "failed")
        self.assertEqual(task["status"], "blocked")
        outcomes = [a["outcome"] for a in st["history"]]
        self.assertEqual(outcomes[:2], ["output", "output"], "asked again in the same session first")
        self.assertEqual(st["history"][1]["session"], st["history"][0]["session"])
        self.assertNotIn("verification", task)  # nothing was verified: the half-done plan isn't checked
        self.assertNotIn("✔", render_report(task).splitlines()[0])

    def test_report_asked_again_in_the_same_session_can_succeed(self):
        self.s.plan = lambda req: plan([("admin-ui", "modify")], [step("admin-ui")])
        calls = []

        def flaky(req):
            calls.append(req.session_id)
            if len(calls) == 1:
                write(req.cwd, "src/change.js", "// changed\n")
                return {"summary": "no status"}
            return {"status": "done", "summary": "edited", "files_changed": ["src/change.js"]}
        self.s.step = flaky
        task = self.eng.handle("change the ui", self.ws.ui)["task"]
        self.assertEqual(task["status"], "done")
        self.assertEqual(calls[1], task["steps"][0]["session_id"])
        self.assertEqual(task["verification"]["level"], "tool_verified")

    def test_malformed_plan_falls_through_to_the_next_provider(self):
        good = plan([("admin-ui", "inspect")], [])
        good["intent"], good["answer"] = "answer", "It works like this."
        seen = []

        def p(req):
            seen.append(req)
            return {"intent": "answer"} if len(seen) == 1 else good
        self.s.plan = p
        task = self.eng.handle("how does the ui work?", self.ws.ui)["task"]
        self.assertEqual(task["status"], "answered")
        self.assertEqual(len(seen), 2)
        failed = [e for e in self.eng.store.events(task["id"]) if e["event"] == "thinking_failed"]
        self.assertIn("didn't match the required format", failed[0]["error"])

    def test_verified_by_tests(self):
        self.s.plan = lambda req: plan([("admin-ui", "modify")], [step("admin-ui")])
        self.s.step = editing()
        res = self.eng.handle("change the ui", self.ws.ui)
        v = res["task"]["verification"]
        self.assertEqual((v["level"], v["tested"], v["review"], v["gaps"]), ("tool_verified", ["admin-ui"], "approved", []))
        self.assertIn("verified by tests", res["report"].splitlines()[0])
        self.assertIn("(verified by tests)", self.notes[-1][0])

    def test_unavailable_review_is_not_approval(self):
        # admin-ui without a test command, and no provider can review: the change is NOT verified.
        write(self.ws.ui, "package.json", json.dumps({"name": "admin-ui"}))
        self.eng = make_engine(self.ws, self.s, notes=self.notes)
        self.s.plan = lambda req: plan([("admin-ui", "modify")], [step("admin-ui")])
        self.s.step = editing()
        self.s.verify = lambda req: {"summary": "missing verdict"}  # malformed on every provider
        res = self.eng.handle("change the ui", self.ws.ui)
        task = res["task"]
        self.assertEqual(task["status"], "done")  # the work finished ...
        v = task["verification"]
        self.assertEqual(v["level"], "unverified")  # ... but nothing confirmed it
        self.assertEqual(v["review"], "unavailable")
        self.assertTrue(any("no test command ran" in g for g in v["gaps"]))
        self.assertTrue(any("review could not run" in g for g in v["gaps"]))
        self.assertIn("NOT verified", res["report"].splitlines()[0])
        self.assertIn("Verified", res["report"])
        self.assertIn("NOT verified", render_status(task, {}))

    def test_passing_checks_still_verify_when_review_is_unavailable(self):
        self.s.plan = lambda req: plan([("admin-ui", "modify")], [step("admin-ui")])
        self.s.step = editing()
        self.s.verify = lambda req: {"summary": "missing verdict"}
        v = self.eng.handle("change the ui", self.ws.ui)["task"]["verification"]
        self.assertEqual(v["level"], "tool_verified")
        self.assertEqual(v["review"], "unavailable")
        self.assertTrue(any("review could not run" in g for g in v["gaps"]))

    def test_project_without_git_is_never_reported_verified(self):
        loose = os.path.join(self.ws.root, "loose-tool")
        os.makedirs(loose)
        write(loose, "package.json", node_pkg("loose-tool"))
        write(loose, "test.js", "console.log('ok')\n")
        self.eng = make_engine(self.ws, self.s, notes=self.notes)
        self.s.plan = lambda req: plan([("loose-tool", "modify")], [step("loose-tool")])
        self.s.step = editing()
        reviewed = []

        def review(req):
            reviewed.append(req.prompt)
            return {"verdict": "approve", "summary": "fine", "issues": []}
        self.s.review = review
        task = self.eng.handle("change the loose tool", loose)["task"]
        v = task["verification"]
        self.assertEqual(task["status"], "done")
        self.assertEqual(v["level"], "reviewed")  # its test passed and the review approved, but changes are unseen
        self.assertTrue(any("not a git repository" in g for g in v["gaps"]))
        self.assertTrue(any(e["kind"] == "check" and e["project"] == "loose-tool" and e["ok"] for e in task["evidence"]))
        self.assertIn("// changed", reviewed[0])  # the reviewer saw the content of the file the worker named

    def test_failing_checks_after_fix_rounds_are_failed_verification(self):
        self.s.plan = lambda req: plan([("admin-ui", "modify")], [step("admin-ui")])

        def breaking(req):
            write(req.cwd, "test.js", "process.exit(1)\n")
            return {"status": "done", "summary": "edited"}
        self.s.step = breaking
        task = self.eng.handle("change the ui", self.ws.ui)["task"]
        self.assertEqual(task["status"], "partial")
        self.assertEqual(task["verification"]["level"], "failed")
        self.assertEqual(task["verification"]["failed"], ["admin-ui"])

    def test_old_task_records_render_without_verification(self):
        self.s.plan = lambda req: plan([("admin-ui", "modify")], [step("admin-ui")])
        self.s.step = editing()
        task = self.eng.handle("change the ui", self.ws.ui)["task"]
        task.pop("verification")
        task.pop("metrics")
        self.assertNotIn("Verified", render_report(task))
        self.assertIn(task["title"], render_status(task, {}))


if __name__ == "__main__":
    unittest.main()
