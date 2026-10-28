"""Questions have ids; an approval names the plan it approves (plan fingerprint), a changed plan closes the earlier
approval question, structured answers are refused when stale, and every change records which interface made it."""

import unittest

from helpers import Script, Workspace, make_engine, write


def plan(instructions="change refunds", needs_approval=True, summary="changes how refunds are paid out"):
    return {"intent": "change", "title": "Refunds", "projects": [{"name": "admin-ui", "role": "modify", "why": "x"}],
            "steps": [{"id": "1", "project": "admin-ui", "kind": "implement", "instructions": instructions,
                       "depends_on": []}], "questions": [],
            "impact": {"summary": summary, "risk": "high", "needs_approval": needs_approval}}


class ApprovalTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.s = Script()
        self.writes = []
        self.s.step = lambda req: (req.mode == "write" and (self.writes.append(req), write(req.cwd, "src/x.js", "1\n")),
                                   {"status": "done", "summary": "done"})[1]
        self.eng = make_engine(self.ws, self.s)

    def tearDown(self):
        self.ws.cleanup()

    def _waiting(self, p):
        self.s.plan = lambda req: p
        task = self.eng.handle("change refunds", self.ws.ui)["task"]
        self.assertEqual(task["status"], "needs_input")
        return task

    def test_questions_get_stable_ids_and_approvals_carry_the_plan(self):
        task = self._waiting(plan())
        q = task["questions"][0]
        self.assertEqual((q["id"], q["kind"]), ("q1", "approval"))
        self.assertEqual(q["plan_fp"], self.eng._plan_fingerprint(task))
        self.assertEqual(self.eng.store.load(task["id"])["questions"][0]["id"], "q1")

    def test_the_fingerprint_follows_what_would_change(self):
        task = self._waiting(plan())
        fp = self.eng._plan_fingerprint(task)
        task["steps"][0]["instructions"] = "change refunds and payouts"
        self.assertNotEqual(self.eng._plan_fingerprint(task), fp)

    def test_a_structured_approval_of_the_current_plan_runs_it(self):
        task = self._waiting(plan())
        q = task["questions"][0]
        queued, msg = self.eng.prepare_answers(task["id"], {q["id"]: "yes"}, plan_fp=q["plan_fp"])
        self.assertEqual(msg, "")
        done = self.eng.execute(queued)["task"]
        self.assertEqual(len(self.writes), 1)
        self.assertIn(done["status"], ("done", "partial"))

    def test_an_approval_for_another_version_of_the_plan_is_stale(self):
        task = self._waiting(plan())
        q = task["questions"][0]
        with open(self.eng.store.path(task["id"], "task.json"), "rb") as f:
            before = f.read()
        for fp in (None, "0" * 16):
            t, msg = self.eng.prepare_answers(task["id"], {q["id"]: "yes"}, plan_fp=fp)
            self.assertIsNone(t)
            self.assertTrue(msg.startswith("stale"), msg)
        with open(self.eng.store.path(task["id"], "task.json"), "rb") as f:
            self.assertEqual(f.read(), before, "nothing changed")
        self.assertEqual(self.writes, [])

    def test_a_changed_plan_closes_the_earlier_approval_so_one_yes_cant_approve_both(self):
        task = self._waiting(plan())
        old = task["questions"][0]
        self.s.plan = lambda req: plan(instructions="change refunds and payouts", summary="now also payouts")
        # new instructions while the approval is still open: a new plan is made, and asked about again
        t, msg = self.eng.prepare_resume(task["id"], "continue, but also handle payouts", cwd=self.ws.ui)
        task = self.eng.execute(t)["task"]
        self.assertEqual(task["status"], "needs_input")
        approvals = [q for q in task["questions"] if q["kind"] == "approval"]
        self.assertEqual(len(approvals), 2)
        self.assertEqual(approvals[0]["id"], old["id"])
        self.assertTrue(approvals[0].get("superseded"))
        open_ = [q for q in approvals if not q.get("answer")]
        self.assertEqual([q["id"] for q in open_], [approvals[1]["id"]])
        self.assertIn("now also payouts", open_[0]["text"])
        t, msg = self.eng.prepare_answers(task["id"], {old["id"]: "yes"}, plan_fp=old["plan_fp"])
        self.assertTrue(msg.startswith("stale"), msg)
        self.assertEqual(self.writes, [])

    def test_answering_a_closed_or_unknown_question_is_stale(self):
        task = self._waiting(plan())
        for qid in ("q9", "nope"):
            t, msg = self.eng.prepare_answers(task["id"], {qid: "yes"})
            self.assertTrue(msg.startswith("stale"), msg)

    def test_a_structured_no_cancels_without_changes(self):
        task = self._waiting(plan())
        q = task["questions"][0]
        t, msg = self.eng.prepare_answers(task["id"], {q["id"]: "no"}, plan_fp=q["plan_fp"])
        self.assertIsNone(t)
        self.assertIn("Cancelled", msg)
        self.assertEqual(self.eng.store.load(task["id"])["status"], "cancelled")
        self.assertEqual(self.writes, [])

    def test_answers_by_id_wait_until_every_question_is_answered(self):
        task = self.eng.store.new_task("pick", self.ws.ui)
        task.update(status="needs_input", questions=[{"text": "Which colour?", "kind": "business", "answer": None},
                                                     {"text": "Which font?", "kind": "business", "answer": None}])
        self.eng.store.save(task)
        t, msg = self.eng.prepare_answers(task["id"], {"q1": "blue"})
        self.assertIsNone(t)
        self.assertIn("1 question still open", msg)
        t, msg = self.eng.prepare_answers(task["id"], {"q2": "serif"})
        self.assertEqual((t["status"], msg), ("queued", ""))
        self.assertEqual([q["answer"] for q in t["questions"]], ["blue", "serif"])


class ActorTest(unittest.TestCase):
    def test_changes_record_the_interface_that_made_them(self):
        ws, s = Workspace(), Script()
        try:
            eng = make_engine(ws, s)
            eng.actor = "desktop"
            s.plan = lambda req: plan()
            task = eng.handle("change refunds", ws.ui)["task"]
            eng.prepare_answer(task["id"], "no")
            events = {e["event"]: e for e in eng.store.events(task["id"])}
            self.assertEqual(events["created"]["actor"], "desktop")
            self.assertEqual(events["cancelled"]["actor"], "desktop")
        finally:
            ws.cleanup()


if __name__ == "__main__":
    unittest.main()
