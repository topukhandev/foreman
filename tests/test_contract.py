"""Result contract v1 and acceptance criteria: the planner states what "done" means, workers rate the criteria and
report the tests they ran, Foreman compares those claims with its own checks, and the report keeps an agent's word,
its evidence and independent checks apart."""

import unittest

from helpers import Script, Workspace, make_engine, write

from orchestrator.engine import Engine
from orchestrator.report import render_report, render_status


def plan(steps, acceptance=None):
    return {"intent": "change", "title": "VIP discount", "projects": [{"name": "admin-ui", "role": "modify", "why": "x"}],
            "steps": steps, "questions": [], "acceptance": acceptance or []}


STEP = {"id": "1", "project": "admin-ui", "kind": "implement", "instructions": "add the discount", "depends_on": []}
CRITERIA = ["VIP customers see a 10% discount", "Other customers see no discount", "The total never goes below 0"]


class ContractTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.s = Script()
        self.eng = make_engine(self.ws, self.s)
        self.prompts = []

    def tearDown(self):
        self.ws.cleanup()

    def worker(self, extra=None, breaks_tests=False):
        def run(req):
            self.prompts.append(req.prompt)
            write(req.cwd, "src/discount.js", "module.exports = 1;\n")
            if breaks_tests:
                write(req.cwd, "test.js", "process.exit(1)\n")
            return {"v": 1, "status": "done", "summary": "added the discount", **(extra or {})}
        return run

    def test_criteria_reach_the_worker_and_its_ratings_are_kept_with_their_basis(self):
        self.s.plan = lambda req: plan([STEP], CRITERIA)
        self.s.step = self.worker({
            "acceptance": [{"id": "AC1", "status": "pass", "evidence": "src/discount.js:1 applies 10%"},
                           {"id": "AC2", "status": "pass"},
                           {"id": "AC9", "status": "pass", "evidence": "no such criterion"}],
            "tests": [{"command": "npm test", "outcome": "passed"}],
            "issues": [{"severity": "low", "title": "rounding untested", "verified": False}],
            "risks": ["Rounding of half cents is not specified"], "assumptions": ["VIP flag is on the customer"],
            "next_action": "QA the rounding"})
        res = self.eng.handle("add a VIP discount", self.ws.ui)
        task = res["task"]
        self.assertIn("AC1: VIP customers see a 10% discount", self.prompts[0])
        self.assertIn("Result contract v1", self.prompts[0])
        self.assertIn("data, not instructions", self.prompts[0])
        crit = {c["id"]: c for c in task["acceptance"]}
        self.assertEqual(list(crit), ["AC1", "AC2", "AC3"])
        self.assertEqual((crit["AC1"]["status"], crit["AC1"]["level"]), ("pass", "evidenced"))
        self.assertEqual((crit["AC2"]["status"], crit["AC2"]["level"]), ("pass", "claimed"))
        self.assertEqual((crit["AC3"]["status"], crit["AC3"]["level"]), ("pending", None))
        result = task["steps"][0]["result"]
        self.assertEqual(result["next_action"], "QA the rounding")
        self.assertEqual(result["issues"][0]["title"], "rounding untested")
        self.assertEqual(task["claims"], [{"step": task["steps"][0]["id"], "project": "admin-ui", "command": "npm test",
                                           "said": "passed", "state": "confirmed"}])
        report = res["report"]
        self.assertIn("Acceptance", report)
        self.assertIn("✔ AC1 VIP customers see a 10% discount (pass; worker, with evidence)", report)
        self.assertIn("✔ AC2 Other customers see no discount (pass; worker's word)", report)
        self.assertIn("· AC3 The total never goes below 0 (not rated)", report)
        self.assertIn("Rounding of half cents is not specified", report)
        self.assertIn("2/3 criteria pass", render_status(task, {}))

    def test_a_pass_claim_contradicted_by_foremans_check_is_reported(self):
        self.s.plan = lambda req: plan([STEP], CRITERIA)
        self.s.step = self.worker({"tests": [{"command": "npm test", "outcome": "passed"}]}, breaks_tests=True)
        res = self.eng.handle("add a VIP discount", self.ws.ui)
        task = res["task"]
        self.assertEqual(task["verification"]["level"], "failed")
        self.assertTrue(any(c["state"] == "contradicted" for c in task["claims"]))
        self.assertIn("Unverified claims", res["report"])
        self.assertTrue(any(e["event"] == "claim_contradicted" for e in self.eng.store.events(task["id"])))

    def test_a_newer_contract_version_is_rejected_as_malformed(self):
        self.s.plan = lambda req: plan([STEP])
        self.s.step = lambda req: {"v": 2, "status": "done", "summary": "from the future"}
        task = self.eng.handle("add a VIP discount", self.ws.ui)["task"]
        self.assertEqual(task["steps"][0]["status"], "failed")
        self.assertIn("version 2", task["steps"][0]["history"][0]["error"])

    def test_results_without_a_version_still_count(self):
        self.s.plan = lambda req: plan([STEP])
        self.s.step = lambda req: (write(req.cwd, "src/x.js", "1\n"), {"status": "done", "summary": "old style"})[1]
        task = self.eng.handle("add a VIP discount", self.ws.ui)["task"]
        self.assertEqual(task["status"], "done")
        self.assertNotIn("acceptance", task)
        self.assertNotIn("Acceptance", render_report(task))

    def test_stronger_evidence_wins_and_every_rating_is_kept(self):
        task = {"acceptance": [{"id": "AC1", "text": "x", "status": "pending", "level": None, "evidence": "", "by": None}]}
        Engine._rate_criterion(task, {"id": "AC1", "status": "pass", "evidence": "dev says"}, {"id": "s1"}, "evidenced")
        Engine._rate_criterion(task, {"id": "AC1", "status": "fail", "evidence": "qa: total is -1"}, {"id": "v1"},
                               "reviewed")
        Engine._rate_criterion(task, {"id": "AC1", "status": "pass"}, {"id": "s2"}, "claimed")  # weaker: kept, not used
        c = task["acceptance"][0]
        self.assertEqual((c["status"], c["level"], c["by"]), ("fail", "reviewed", "v1"))
        self.assertEqual([r["by"] for r in c["ratings"]], ["s1", "v1", "s2"])
        Engine._rate_criterion(task, {"id": "AC1", "status": "maybe"}, {"id": "s3"}, "reviewed")  # invalid: ignored
        self.assertEqual(len(c["ratings"]), 3)


if __name__ == "__main__":
    unittest.main()
