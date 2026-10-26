"""Questions to the user: asked in plain words (planner and workers), options shown in full."""

import unittest

from helpers import Script, Workspace, make_engine
from test_lifecycle import with_mode
from test_scenarios import editing_worker, plan, step

from orchestrator.brain import PLANNING_RULES, QUESTION_RULE
from orchestrator.report import render_report


class PlainQuestionsTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.s = Script()
        self.eng = make_engine(self.ws, self.s)

    def tearDown(self):
        self.ws.cleanup()

    def test_planner_and_workers_are_told_to_ask_in_plain_words(self):
        self.assertIn(QUESTION_RULE, PLANNING_RULES)
        self.assertIn("do not ask again", QUESTION_RULE)
        self.s.plan = lambda req: with_mode(plan([("orders-api", "modify")], [step("1", "orders-api")]), "development")
        prompts, edit = [], editing_worker()
        self.s.step = lambda req: (prompts.append(req.prompt), edit(req))[1]
        self.eng.handle("add currency", self.ws.api)
        self.assertTrue(prompts and "plain, everyday English" in prompts[0])

    def test_report_lists_each_option_on_its_own_line(self):
        self.s.plan = lambda req: {**plan([], [], intent="answer", answer="x"), "questions": [
            {"text": "Some products only have a French title. What should happen to them?", "kind": "business",
             "options": ["Translate the titles into English", "Keep them without a title"],
             "default": "Translate the titles into English"}]}
        report = render_report(self.eng.handle("merge the files", self.ws.api)["task"])
        self.assertIn("  1. Some products only have a French title. What should happen to them?\n"
                      "     a) Translate the titles into English (recommended)\n"
                      "     b) Keep them without a title\n", report)


if __name__ == "__main__":
    unittest.main()
