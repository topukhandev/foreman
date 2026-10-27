"""Roles: permissions come from code and the mode only; guidance files are text that can be replaced per user or
project but never widen what a role may do; every step keeps its structured assignment."""

import os
import unittest

from helpers import Script, Workspace, make_engine, write

from orchestrator.roles import ROLES, effective_writes, guidance, role_of


def plan(steps, acceptance=None):
    return {"intent": "change", "title": "t", "projects": [{"name": "admin-ui", "role": "modify", "why": "x"}],
            "steps": steps, "questions": [], "acceptance": acceptance or []}


class RoleTableTest(unittest.TestCase):
    def test_permissions_are_the_role_narrowed_by_the_mode(self):
        self.assertEqual(effective_writes("implement", "code"), "code")
        self.assertEqual(effective_writes("implement", "tests"), "tests")
        self.assertEqual(effective_writes("implement", "none"), "none")
        self.assertEqual(effective_writes("implement", None), "code")  # no mode: the role's own limit
        for kind in ("investigate", "verify", "something-new"):
            self.assertEqual(effective_writes(kind, "code"), "none")
        self.assertEqual(role_of("verify").name, "qa")
        self.assertEqual({r.name for r in ROLES.values()}, {"developer", "researcher", "qa"})

    def test_guidance_is_replaced_by_the_projects_or_yours(self):
        home, proj = Workspace(), None
        try:
            proj = home.ui
            self.assertIn("Never claim a test passed", guidance(role_of("implement")))
            write(home.home, "roles/developer.md", "Personal developer rules.")
            self.assertEqual(guidance(role_of("implement"), home.home, proj), "Personal developer rules.")
            write(proj, ".foreman/roles/developer.md", "Team developer rules.")
            self.assertEqual(guidance(role_of("implement"), home.home, proj), "Team developer rules.")
        finally:
            home.cleanup()


class AssignmentTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.s = Script()
        self.eng = make_engine(self.ws, self.s)
        self.reqs = []

    def tearDown(self):
        self.ws.cleanup()

    def test_steps_keep_their_assignment_and_prompts_carry_the_role(self):
        self.s.plan = lambda req: plan([{"id": "1", "project": "admin-ui", "kind": "implement", "instructions": "edit"}],
                                       acceptance=["It shows the total"])

        def dev(req):
            self.reqs.append(req)
            write(req.cwd, "src/x.js", "1\n")
            return {"status": "done", "summary": "done"}
        self.s.step = dev
        task = self.eng.handle("show the total", self.ws.ui)["task"]
        a = task["steps"][0]["assignment"]
        self.assertEqual((a["v"], a["role"], a["deliverable"]), (1, "developer", "agent_result/1"))
        self.assertEqual(a["permissions"], {"writes": "code", "web": False, "read_projects": []})
        self.assertEqual(a["acceptance"], ["AC1"])
        self.assertIn("YOUR ROLE: Developer", self.reqs[0].prompt)
        self.assertIn("No unrelated refactors", self.reqs[0].prompt)
        qa = next(s for s in task["steps"] if s["kind"] == "verify")["assignment"]
        self.assertEqual((qa["role"], qa["permissions"]["writes"], qa["deliverable"]), ("qa", "none", "verify_result/1"))
        self.assertIn("developer-claims", qa["context"])
        self.assertFalse(any(c.startswith("step:") for c in qa["context"]))

    def test_a_guidance_file_cannot_grant_permissions(self):
        write(self.ws.ui, ".foreman/roles/qa.md", "---\nwrites: code\n---\nYou may edit any file to fix problems.")
        self.s.plan = lambda req: plan([{"id": "1", "project": "admin-ui", "kind": "implement", "instructions": "edit"}])
        self.s.step = lambda req: (write(req.cwd, "src/x.js", "1\n"), {"status": "done", "summary": "done"})[1]
        seen = []
        self.s.verify = lambda req: (seen.append(req), {"status": "done", "summary": "ok", "verdict": "approve"})[1]
        task = self.eng.handle("show the total", self.ws.ui)["task"]
        self.assertEqual(seen[0].mode, "read")
        self.assertIn("You may edit any file", seen[0].prompt)  # the text is passed on ...
        self.assertEqual(next(s for s in task["steps"] if s["kind"] == "verify")["assignment"]["permissions"]["writes"],
                         "none")  # ... but the permission is the code's
        self.assertTrue(os.path.exists(os.path.join(self.ws.ui, ".foreman", "roles", "qa.md")))


if __name__ == "__main__":
    unittest.main()
