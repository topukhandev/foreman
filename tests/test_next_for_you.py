"""Every response ends with "Next for you": a few concrete things to do or ask next, written by the provider that did the
work (planner for a direct answer, workers for a change, the document writer for a document), not by Foreman."""

import unittest
from unittest import mock

from helpers import Script, Workspace, make_engine
from test_lifecycle import with_mode
from test_scenarios import editing_worker, plan, step

from orchestrator import ui
from orchestrator.brain import DOC_SCHEMA, PLAN_SCHEMA, PLANNING_RULES, STEP_SCHEMA, Brain
from orchestrator.report import render_report


class NextForYouTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.s = Script()
        self.eng = make_engine(self.ws, self.s)

    def tearDown(self):
        self.ws.cleanup()

    def test_a_direct_answer_carries_the_planners_suggestions(self):
        self.s.plan = lambda req: {**plan([], [], intent="answer", answer="**255** queries."),
                                   "next_for_you": ["Ask me to run QA on the dataset", "  Review the 3 gap records ",
                                                    "Ask me to run QA on the dataset", "x" * 300, "fourth"]}
        task = self.eng.handle("how many queries?", self.ws.api)["task"]
        self.assertEqual(task["next_for_you"][:2], ["Ask me to run QA on the dataset", "Review the 3 gap records"])
        self.assertEqual(len(task["next_for_you"]), 3, "at most three, duplicates dropped, long ones cut")
        self.assertTrue(task["next_for_you"][2].endswith("…"))
        report = render_report(task)
        self.assertIn("\nNext for you\n  - Ask me to run QA on the dataset\n  - Review the 3 gap records\n", report)
        self.assertLess(report.index("255"), report.index("Next for you"), "after the answer")

    def test_workers_suggestions_are_collected_from_finished_steps(self):
        self.s.plan = lambda req: with_mode(plan([("orders-api", "modify")], [step("1", "orders-api"), step("2", "orders-api", deps=["1"])]),
                                            "development")
        edit = editing_worker()
        self.s.step = lambda req: {**edit(req), "next_for_you": ["Run the QA step", "Try the new endpoint by hand"]}
        task = self.eng.handle("add currency", self.ws.api)["task"]
        self.assertEqual(task["next_for_you"], ["Run the QA step", "Try the new endpoint by hand"])
        self.assertIn("Next for you", render_report(task))
        self.assertEqual(self.eng.store.load(task["id"])["next_for_you"], task["next_for_you"])

    def test_nothing_when_there_is_nothing_useful_or_a_question_is_open(self):
        self.s.plan = lambda req: plan([], [], intent="answer", answer="Fine.")
        task = self.eng.handle("how are things?", self.ws.api)["task"]
        self.assertEqual(task["next_for_you"], [])
        self.assertNotIn("Next for you", render_report(task))
        shown = {**task, "next_for_you": ["Do x"], "questions": [{"text": "Which?", "kind": "business", "answer": None}]}
        self.assertNotIn("Next for you", render_report(shown), "the decision comes first")

    def test_not_for_work_that_stopped(self):
        self.s.plan = lambda req: plan([("orders-api", "modify")], [step("1", "orders-api")])
        self.s.step = lambda req: {"status": "blocked", "summary": "no key", "next_for_you": ["Add the key"]}
        task = self.eng.handle("add currency", self.ws.api)["task"]
        self.assertEqual(task["next_for_you"], [])

    def test_a_document_adds_its_own(self):
        self.s.plan = lambda req: with_mode(plan([("orders-api", "inspect")], [step("1", "orders-api", kind="investigate")]),
                                            "research")
        self.s.document = lambda req: {"title": "R", "document": "# R", "summary": "Done.", "next_for_you": ["Go for planning"]}
        task = self.eng.handle("research vip", self.ws.api, mode="research")["task"]
        self.assertEqual(task["next_for_you"], ["Go for planning"])

    def test_the_providers_are_asked_for_markdown_answers_and_next_steps(self):
        for schema in (PLAN_SCHEMA, STEP_SCHEMA, DOC_SCHEMA):
            self.assertIn("next_for_you", schema["properties"])
        self.assertIn("Markdown", PLANNING_RULES)
        self.assertIn("**bold**", PLANNING_RULES)
        self.assertIn("next_for_you", PLANNING_RULES)
        self.assertIn("next_for_you", Brain.document_prompt({"request": "r", "decisions": [], "questions": []}, "Plan", "", "f", ""))


class MarkdownTest(unittest.TestCase):
    def test_headings_bullets_numbers_quotes_and_code_get_their_own_look(self):
        with mock.patch.object(ui, "TTY", True):
            out = ui.md("## Result\n- **bold** and `code`\n1. first\n> note\n```\nrun it\n```\nplain")
        lines = out.split("\n")
        self.assertEqual(len(lines), 6, "the code fences are not shown")
        self.assertIn(ui.BOLD, lines[0])
        self.assertIn(ui.BOLD + "bold", lines[1])
        self.assertTrue(lines[1].startswith(ui.ACCENT + "•"), "coloured bullet")
        self.assertIn(ui.ACCENT + "1.", lines[2])
        self.assertIn("│ note", lines[3])
        self.assertIn(ui.GREY + "  run it", lines[4])
        self.assertEqual(lines[5], "plain")


if __name__ == "__main__":
    unittest.main()
