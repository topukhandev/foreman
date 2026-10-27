"""Decision memory: a later answer to the same question supersedes the earlier one (history kept), and a question the
project already decided is answered from .foreman/decisions.md instead of being asked again (once per worker)."""

import os
import unittest

from helpers import Script, Workspace, make_engine, write

from orchestrator.knowledge import ProjectKnowledge

Q = "Should refunds above the order total be allowed?"


def plan(steps, questions=None):
    return {"intent": "change", "title": "Refunds", "projects": [{"name": "admin-ui", "role": "modify", "why": "x"}],
            "steps": steps, "questions": questions or []}


STEP = {"id": "1", "project": "admin-ui", "kind": "implement", "instructions": "refunds", "depends_on": []}


class DecisionLogTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.kn = ProjectKnowledge(self.ws.ui)

    def tearDown(self):
        self.ws.cleanup()

    def test_a_new_answer_supersedes_the_old_one_and_history_is_kept(self):
        self.kn.add_decision(Q, "No", "3", "refunds")
        self.kn.add_decision(Q, "No", "4", "refunds")  # the same answer again: not repeated
        self.kn.add_decision("should REFUNDS above the order total be allowed", "Yes, up to 10%", "7", "refunds")
        self.kn.add_decision("Which currency?", "EUR", "7")
        with open(os.path.join(self.ws.ui, ".foreman", "decisions.md")) as f:
            text = f.read()
        self.assertEqual(text.count(f"{Q} → No"), 1)
        self.assertIn("[superseded", text)
        self.assertIn("by task #7]", text)
        current = self.kn.decisions()
        self.assertEqual(len(current), 2)
        self.assertTrue(current[0].endswith("→ Yes, up to 10% (task #7)"))
        self.assertEqual(self.kn.known_answer(Q), "Yes, up to 10%")
        self.assertEqual(self.kn.known_answer("Which currency"), "EUR")
        self.assertIsNone(self.kn.known_answer("Which timezone?"))


class AskOnceTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.s = Script()
        self.eng = make_engine(self.ws, self.s)
        ProjectKnowledge(self.ws.ui).add_decision(Q, "No, cap them at the total", "1", "refunds")

    def tearDown(self):
        self.ws.cleanup()

    def test_the_planner_is_not_allowed_to_ask_a_decided_question_again(self):
        self.s.plan = lambda req: plan([STEP], [{"text": Q, "kind": "business"}])
        self.s.step = lambda req: (write(req.cwd, "src/r.js", "1\n"), {"status": "done", "summary": "capped"})[1]
        task = self.eng.handle("allow refunds", self.ws.ui)["task"]
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["questions"], [])
        self.assertTrue(any("Answered from .foreman/decisions.md" in d["text"] for d in task["decisions"]))

    def test_a_worker_gets_the_recorded_answer_in_its_session_once(self):
        calls = []

        def worker(req):
            calls.append(req)
            if len(calls) <= 2:  # asks, then (contrary) asks the same again
                return {"status": "needs_input", "summary": "need a rule",
                        "questions": [{"text": Q, "kind": "business"}]}
            write(req.cwd, "src/r.js", "1\n")
            return {"status": "done", "summary": "capped"}
        self.s.plan = lambda req: plan([STEP])
        self.s.step = worker
        task = self.eng.handle("allow refunds", self.ws.ui)["task"]
        self.assertIn("No, cap them at the total", calls[1].prompt)
        self.assertEqual(calls[1].session_id, task["steps"][0]["session_id"])
        self.assertEqual(task["status"], "needs_input", "asked a second time, it goes to the user")
        open_q = [q for q in task["questions"] if not q.get("answer")]
        self.assertEqual([q["text"] for q in open_q], [Q])
        task = self.eng.answer(task["id"], "No, cap them at the total")["task"]
        self.assertEqual(task["status"], "done")


if __name__ == "__main__":
    unittest.main()
