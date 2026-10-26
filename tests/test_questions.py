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


class QuestionOptionsTest(unittest.TestCase):
    def test_labels_descriptions_header_and_recommendation_first(self):
        from orchestrator.engine import question_options
        q = {"header": "Foreign titles here", "default": "Translate", "options": [
            {"label": "Keep as is", "description": "107 products keep a French title."},
            {"label": "Translate", "description": "Every product keeps a title, in English."}]}
        self.assertEqual(question_options(q), {"options": ["Translate", "Keep as is"],
                                               "descriptions": ["Every product keeps a title, in English.",
                                                                "107 products keep a French title."],
                                               "default": "Translate", "header": "Foreign titl"[:12]})
        plain = question_options({"options": ["A", "B"], "default": "B"})
        self.assertEqual((plain["options"], plain["descriptions"]), (["B", "A"], ["", ""]))
        long_default = question_options({"options": ["Drop them"], "default": "Drop them; the total becomes 5,893"})
        self.assertEqual(long_default["default"], "Drop them", "a recommendation written out in full maps to its label")


class AskFlowTest(unittest.TestCase):
    """The chat's question panels, with ui.choose scripted."""

    def run_flow(self, questions, picks):
        from unittest import mock
        from orchestrator import ui
        from orchestrator.chat import Chat
        ws = Workspace()
        self.addCleanup(ws.cleanup)
        chat = Chat(ws.api, engine=make_engine(ws, Script()))
        seen = []

        def choose(title, body, options, **kw):
            seen.append((title, list(options), kw))
            pick = picks.pop(0)
            return {"<": ui.PREV, ">": ui.NEXT}.get(pick, pick)
        with mock.patch("orchestrator.ui.choose", side_effect=choose):
            return chat._ask(questions), seen

    def q(self, header, *opts):
        return {"text": f"{header}?", "kind": "business", "header": header, "options": list(opts),
                "descriptions": [f"about {o}" for o in opts], "default": opts[0]}

    def test_one_question_needs_no_review(self):
        answers, seen = self.run_flow([self.q("Titles", "Translate", "Keep")], ["Translate (Recommended)"])
        self.assertEqual(answers, ["Translate"])
        title, options, kw = seen[0]
        self.assertEqual((title, kw["descriptions"], kw["nav"]), ("Titles", ["about Translate", "about Keep"], False))

    def test_several_questions_have_chips_arrows_and_a_review_step(self):
        qs = [self.q("Titles", "Translate", "Keep"), self.q("File", "One file", "Two files")]
        answers, seen = self.run_flow(qs, ["Keep", ">", "<", "my own idea", "Submit answers"])
        self.assertEqual(answers, ["Keep", "my own idea"])
        self.assertIn("Titles", seen[0][2]["tabs"])
        self.assertEqual([t for t, _, _ in seen], ["Titles", "File", "Review your answers", "File", "Review your answers"],
                         "→ from the last question opens the review; ← from the review goes back to it")
        self.assertEqual(seen[3][2]["start"], 0, "a typed answer was not given yet there")

    def test_submit_with_a_missing_answer_goes_to_it(self):
        qs = [self.q("Titles", "Translate", "Keep"), self.q("File", "One file", "Two files")]
        answers, seen = self.run_flow(qs, [">", ">", "Submit answers", "Translate (Recommended)",
                                           "One file (Recommended)", "Submit answers"])
        self.assertEqual(seen[3][0], "Titles", "Submit with answers missing opens the first one missing")
        self.assertEqual(answers, ["Translate", "One file"])


if __name__ == "__main__":
    unittest.main()
