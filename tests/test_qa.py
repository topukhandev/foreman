"""The QA verification workflow: risk-adaptive gate, independent QA session, verified problems back to the developer's
session and re-verified in QA's own session (bounded), QA-requested tests run by Foreman, honest outcomes when QA
can't run, resumption, and the two approval policies (high-impact plans, new dependencies)."""

import json
import unittest

from helpers import Script, Workspace, make_engine, node_pkg, write

from orchestrator.deps import declared


def plan(steps, projects=(("admin-ui", "modify"),), impact=None, acceptance=None):
    p = {"intent": "change", "title": "Orders total", "projects": [{"name": n, "role": r, "why": "x"} for n, r in projects],
         "steps": steps, "questions": [], "acceptance": acceptance or []}
    if impact:
        p["impact"] = impact
    return p


def step(project="admin-ui", id_="1", weight="standard"):
    return {"id": id_, "project": project, "kind": "implement", "instructions": "change it", "depends_on": [],
            "weight": weight}


def qa(verdict="approve", issues=None, **extra):
    return {"status": "done", "summary": f"QA: {verdict}", "verdict": verdict, "issues": issues or [], **extra}


BUG = {"severity": "high", "title": "total can go negative", "evidence": "src/change.js:1", "verified": True,
       "repro": "order with refund > total"}


class QAWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.s = Script()
        self.eng = make_engine(self.ws, self.s)
        self.qa_calls = []
        self.dev_calls = []

    def tearDown(self):
        self.ws.cleanup()

    def developer(self, files=("src/change.js",)):
        def run(req):
            self.dev_calls.append(req)
            for f in files:
                write(req.cwd, f, f"// change {len(self.dev_calls)}\n")
            return {"status": "done", "summary": f"edit {len(self.dev_calls)}", "files_changed": list(files),
                    "tests": [{"command": "npm test", "outcome": "passed"}]}
        return run

    def scripted_qa(self, *results):
        it = iter(results)

        def run(req):
            self.qa_calls.append(req)
            return next(it)
        return run

    def test_qa_runs_independently_and_sees_claims_not_reasoning(self):
        self.s.plan = lambda req: plan([step()], acceptance=["Totals are shown with two decimals"])
        self.s.step = self.developer()
        self.s.verify = self.scripted_qa(qa(acceptance=[{"id": "AC1", "status": "pass", "evidence": "format() at :1"}]))
        task = self.eng.handle("format the totals", self.ws.ui)["task"]
        self.assertEqual(task["status"], "done")
        v = [s for s in task["steps"] if s["kind"] == "verify"]
        self.assertEqual(len(v), 1)
        dev_provider = task["steps"][0]["provider"]
        self.assertNotEqual(v[0]["provider"], dev_provider, "QA on a different provider than the developer")
        req = self.qa_calls[0]
        self.assertEqual(req.mode, "read")
        self.assertIsNone(req.session_id)  # a fresh session: not the developer's, not one of yours
        self.assertIn("DEVELOPER REPORTS (claims to check, not facts)", req.prompt)
        self.assertIn("says `npm test` passed", req.prompt)
        self.assertIn("FOREMAN'S OWN CHECKS", req.prompt)
        self.assertIn("+// change 1", req.prompt)  # the diff
        self.assertNotIn("RESULTS OF OTHER STEPS", req.prompt)
        self.assertEqual(task["acceptance"][0]["level"], "reviewed")
        self.assertEqual(task["verification"]["level"], "tool_verified")
        self.assertEqual(task["verification"]["review"], "approved")
        self.assertEqual(task["gate"]["tier"], 1)
        self.assertTrue(any(s.get("reviewer") for s in task["sessions"]))

    def test_verified_problem_goes_back_to_the_developer_and_qa_rechecks_in_its_session(self):
        self.s.plan = lambda req: plan([step()])
        self.s.step = self.developer()
        self.s.verify = self.scripted_qa(qa("revise", [BUG]), qa("approve"))
        task = self.eng.handle("show the total", self.ws.ui)["task"]
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["fix_rounds"], 1)
        fix = next(s for s in task["steps"] if s["id"].startswith("fix"))
        self.assertIn("QA: [high] total can go negative", fix["instructions"])
        self.assertIn("repro: order with refund > total", fix["instructions"])
        self.assertEqual(self.dev_calls[1].session_id, task["steps"][0]["session_id"], "fix in the developer's session")
        self.assertEqual(self.qa_calls[1].session_id, [s for s in task["steps"] if s["kind"] == "verify"][0]["session_id"],
                         "re-verification continues QA's own session")
        self.assertIn("RE-VERIFY", self.qa_calls[1].prompt)
        self.assertIn("total can go negative", self.qa_calls[1].prompt)
        self.assertEqual(task["qa"]["rounds"], 2)
        self.assertEqual(task["verification"]["level"], "tool_verified")

    def test_suspected_issues_are_reported_not_fixed(self):
        self.s.plan = lambda req: plan([step()])
        self.s.step = self.developer()
        self.s.verify = self.scripted_qa(qa("revise", [{**BUG, "verified": False}, {"severity": "low", "title": "naming",
                                                                                     "verified": True}]))
        res = self.eng.handle("show the total", self.ws.ui)
        task = res["task"]
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["fix_rounds"], 0)
        self.assertEqual(task["qa"]["state"], "concerns")
        self.assertIn("QA notes (suspected, not confirmed; not fixed automatically)", res["report"])
        self.assertTrue(any("could not confirm" in g for g in task["verification"]["gaps"]))

    def test_fix_rounds_are_bounded(self):
        self.s.plan = lambda req: plan([step()])
        self.s.step = self.developer()
        self.s.verify = lambda req: (self.qa_calls.append(req), qa("revise", [BUG]))[1]
        task = self.eng.handle("show the total", self.ws.ui)["task"]
        limit = self.eng.cfg.limits.max_fix_rounds
        self.assertEqual(task["status"], "partial")
        self.assertEqual(task["fix_rounds"], limit)
        self.assertEqual(len(self.qa_calls), limit + 1)
        self.assertEqual(task["verification"]["level"], "failed")
        self.assertTrue(any("total can go negative" in r for r in task["remaining"]))

    def test_small_low_risk_change_with_passing_checks_needs_no_qa(self):
        self.s.plan = lambda req: plan([step(weight="light")], impact={"summary": "label", "risk": "low"})
        self.s.step = self.developer()
        self.s.verify = self.scripted_qa()
        res = self.eng.handle("rename a label", self.ws.ui)
        task = res["task"]
        self.assertEqual(self.qa_calls, [])
        self.assertEqual(task["gate"]["tier"], 0)
        self.assertEqual((task["verification"]["level"], task["verification"]["review"]), ("tool_verified", "skipped"))
        self.assertIn("no QA needed", res["report"])

    def test_small_change_without_a_passing_check_still_gets_qa(self):
        write(self.ws.ui, "package.json", json.dumps({"name": "admin-ui"}))  # no test command
        self.eng = make_engine(self.ws, self.s)
        self.s.plan = lambda req: plan([step(weight="light")], impact={"summary": "label", "risk": "low"})
        self.s.step = self.developer()
        self.s.verify = self.scripted_qa(qa())
        task = self.eng.handle("rename a label", self.ws.ui)["task"]
        self.assertEqual(task["gate"]["tier"], 1)
        self.assertEqual(len(self.qa_calls), 1)
        self.assertEqual(task["verification"]["level"], "reviewed")

    def test_sensitive_files_get_the_risk_checklist_whatever_the_prediction(self):
        self.s.plan = lambda req: plan([step(weight="light")], impact={"summary": "tiny", "risk": "low"})
        self.s.step = self.developer(files=("migrations/001_add_amount.sql",))
        self.s.verify = self.scripted_qa(qa())
        task = self.eng.handle("add the amount column", self.ws.ui)["task"]
        self.assertEqual(task["gate"]["tier"], 2)
        self.assertIn("RISK CHECKLIST", self.qa_calls[0].prompt)

    def test_tests_qa_requests_are_run_by_foreman_under_the_proposed_rules(self):
        write(self.ws.ui, "test/vip.test.js", "process.exit(1)\n")
        self.s.plan = lambda req: plan([step()])
        self.s.step = self.developer()
        self.s.verify = self.scripted_qa(
            qa(tests_requested=[{"project": "admin-ui", "command": "node test/vip.test.js"},
                                {"project": "admin-ui", "command": "node test.js; touch /tmp/x"}]),
            qa())
        task = self.eng.handle("vip totals", self.ws.ui)["task"]
        ran = [e["command"] for e in task["evidence"] if e["kind"] == "check"]
        self.assertIn("node test/vip.test.js", ran)
        self.assertNotIn("node test.js; touch /tmp/x", ran)
        self.assertEqual(task["fix_rounds"], 1, "the failing requested test went back to the developer")

    def test_a_failed_acceptance_criterion_goes_back_to_the_developer(self):
        self.s.plan = lambda req: plan([step()], acceptance=["Refunds never make the total negative"])
        self.s.step = self.developer()
        self.s.verify = self.scripted_qa(qa("revise", acceptance=[{"id": "AC1", "status": "fail",
                                                                    "evidence": "refund path skips the floor"}]),
                                         qa(acceptance=[{"id": "AC1", "status": "pass", "evidence": "max(0, …)"}]))
        task = self.eng.handle("refunds", self.ws.ui)["task"]
        fix = next(s for s in task["steps"] if s["id"].startswith("fix"))
        self.assertIn("acceptance criterion AC1 fails", fix["instructions"])
        self.assertEqual((task["acceptance"][0]["status"], task["acceptance"][0]["level"]), ("pass", "reviewed"))

    def test_qa_that_cannot_run_is_unverified_not_approved(self):
        write(self.ws.ui, "package.json", json.dumps({"name": "admin-ui"}))  # and no test command
        self.eng = make_engine(self.ws, self.s)
        self.s.plan = lambda req: plan([step()])
        self.s.step = self.developer()
        self.s.verify = lambda req: {"summary": "no verdict"}  # malformed everywhere
        res = self.eng.handle("show the total", self.ws.ui)
        task = res["task"]
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["verification"]["level"], "unverified")
        self.assertIn("NOT verified", res["report"].splitlines()[0])
        self.assertNotIn("Stopped at", res["report"])

    def test_qa_question_waits_for_the_user_and_resumes_qas_session(self):
        self.s.plan = lambda req: plan([step()])
        self.s.step = self.developer()
        self.s.verify = self.scripted_qa(
            {"status": "needs_input", "summary": "unclear", "verdict": "revise",
             "questions": [{"text": "Should refunds above the total be allowed?", "kind": "business"}]},
            qa())
        task = self.eng.handle("refunds", self.ws.ui)["task"]
        self.assertEqual(task["status"], "needs_input")
        task = self.eng.answer(task["id"], "No, cap them at the total")["task"]
        self.assertEqual(task["status"], "done")
        verifies = [s for s in task["steps"] if s["kind"] == "verify"]
        self.assertEqual(len(verifies), 1, "the answered QA round was reused, not repeated")
        self.assertEqual(self.qa_calls[1].session_id, verifies[0]["session_id"])
        self.assertIn("cap them at the total", self.qa_calls[1].prompt)

    def test_qa_never_falls_back_to_the_developers_session(self):
        from orchestrator.providers.base import RunResult
        self.s.plan = lambda req: plan([step()])
        self.s.step = self.developer()
        self.s.verify = self.scripted_qa(qa("revise", [BUG]), qa())
        claude, opencode = self.eng.providers["claude"], self.eng.providers["opencode"]
        inner = opencode.handler
        rounds = []

        def qa_on_opencode_until_limited(req):  # QA's provider runs out after its first round
            if req.schema is not None and "verdict" in req.schema["properties"]:
                rounds.append(1)
                if len(rounds) > 1:
                    return RunResult(False, error="You've hit your session limit")
            return inner(req)
        opencode.handler = qa_on_opencode_until_limited
        task = self.eng.handle("show the total", self.ws.ui)["task"]
        dev_sessions = {s["session_id"] for s in task["steps"] if s["kind"] == "implement"}
        qa_calls_on_claude = [c for c in claude.calls if c.schema is not None and "verdict" in c.schema["properties"]]
        self.assertTrue(qa_calls_on_claude, "QA moved to the other provider")
        self.assertTrue(all(c.session_id not in dev_sessions for c in qa_calls_on_claude))
        self.assertNotIn(task["qa"]["session_id"], dev_sessions)

    def test_lint_alone_never_counts_as_verified_by_tests(self):
        write(self.ws.ui, "package.json", json.dumps({"name": "admin-ui"}))  # no test command
        self.eng = make_engine(self.ws, self.s)
        p = plan([step(weight="light")], impact={"summary": "label", "risk": "low"})
        p["checks"] = [{"project": "admin-ui", "command": "node lint.js"}]
        write(self.ws.ui, "lint.js", "process.exit(0)\n")
        self.s.plan = lambda req: p
        self.s.step = self.developer()
        self.s.verify = self.scripted_qa(qa())
        task = self.eng.handle("rename a label", self.ws.ui)["task"]
        self.assertEqual(task["gate"]["tier"], 1, "a passing non-test check doesn't make a change tier 0")
        self.assertEqual(task["verification"]["level"], "reviewed")

    def test_qa_result_is_not_reused_once_the_code_changed(self):
        self.s.plan = lambda req: plan([step()])
        self.s.step = self.developer()
        self.s.verify = self.scripted_qa(qa(), qa())
        task = self.eng.handle("show the total", self.ws.ui)["task"]
        write(self.ws.ui, "src/change.js", "// edited by you afterwards\n")
        task["status"] = "interrupted"
        self.eng.store.save(task)
        self.eng.resume(task["id"])
        self.assertEqual(len(self.qa_calls), 2, "QA looked again at the changed content")

    def test_qa_cut_off_mid_run_resumes_its_session(self):
        self.s.plan = lambda req: plan([step()])
        self.s.step = self.developer()
        self.s.verify = self.scripted_qa(qa(), qa())
        task = self.eng.handle("show the total", self.ws.ui)["task"]
        v = next(s for s in task["steps"] if s["kind"] == "verify")
        v["status"] = "running"  # the process died while QA was running
        v["history"][-1].pop("ended", None)
        task["status"] = "interrupted"
        self.eng.store.save(task)
        task = self.eng.resume(task["id"])["task"]
        self.assertEqual(task["status"], "done")
        self.assertEqual(len([s for s in task["steps"] if s["kind"] == "verify"]), 1)
        self.assertEqual(self.qa_calls[1].session_id, v["session_id"])
        self.assertEqual(len(self.dev_calls), 1, "the finished implementation was not redone")


class ApprovalPolicyTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.s = Script()
        self.eng = make_engine(self.ws, self.s)

    def tearDown(self):
        self.ws.cleanup()

    def test_high_impact_plan_waits_for_approval_in_every_interface(self):
        calls = []
        self.s.plan = lambda req: plan([step()], impact={"summary": "changes how refunds are paid out", "risk": "high",
                                                         "needs_approval": True})
        self.s.step = lambda req: (req.mode == "write" and (calls.append(req), write(req.cwd, "src/x.js", "1\n")),
                                   {"status": "done", "summary": "done"})[1]
        task = self.eng.handle("change refunds", self.ws.ui)["task"]  # confirm_plan is off (CLI / MCP)
        self.assertEqual(task["status"], "needs_input")
        self.assertEqual(calls, [])
        q = task["questions"][0]
        self.assertEqual(q["kind"], "approval")
        self.assertIn("changes how refunds are paid out", q["text"])
        task = self.eng.answer(task["id"], "yes")["task"]
        self.assertEqual(len(calls), 1)
        self.assertIn(task["status"], ("done", "partial"))

    def test_a_replan_that_turns_high_impact_asks_again(self):
        plans = iter([plan([{**step(), "kind": "investigate"}], projects=(("admin-ui", "inspect"),)),
                      plan([step(id_="2")], impact={"summary": "now touches payouts", "risk": "high",
                                                    "needs_approval": True})])
        self.s.plan = lambda req: next(plans)
        writes = []
        self.s.step = lambda req: ({"status": "done", "summary": "found", "dependencies": [
            {"project": self.ws.api, "type": "uses", "evidence": "fetch"}]} if req.mode == "read" else
            (writes.append(1), write(req.cwd, "src/x.js", "1\n"), {"status": "done", "summary": "done"})[2])
        task = self.eng.handle("look into payouts", self.ws.ui)["task"]
        self.assertEqual(task["status"], "needs_input")
        self.assertEqual(writes, [])
        self.assertTrue(any(q["kind"] == "approval" and "now touches payouts" in q["text"] for q in task["questions"]))

    def test_ordinary_complex_change_needs_no_approval(self):
        self.s.plan = lambda req: plan([step()], impact={"summary": "refactor", "risk": "medium"})
        self.s.step = lambda req: (write(req.cwd, "src/x.js", "1\n"), {"status": "done", "summary": "done"})[1]
        task = self.eng.handle("refactor totals", self.ws.ui)["task"]
        self.assertEqual(task["status"], "done")

    def _dep_worker(self, calls):
        def run(req):
            calls.append(req)
            pkg = json.loads(node_pkg("admin-ui"))
            if len(calls) == 1 or "A: Keep it" in req.prompt:
                pkg["dependencies"] = {"left-pad": "^1.3.0"}
            write(req.cwd, "package.json", json.dumps(pkg))
            write(req.cwd, "src/pad.js", f"// {len(calls)}\n")
            return {"status": "done", "summary": "padded" if len(calls) == 1 else "continued"}
        return run

    def test_a_new_dependency_is_never_added_silently(self):
        calls = []
        self.s.plan = lambda req: plan([step()])
        self.s.step = self._dep_worker(calls)
        task = self.eng.handle("pad the totals", self.ws.ui)["task"]
        self.assertEqual(task["status"], "needs_input")
        q = next(q for q in task["questions"] if not q.get("answer"))
        self.assertEqual((q["kind"], q["deps"]), ("dependency", ["left-pad"]))
        self.assertIn("left-pad", q["text"])
        self.assertNotIn("verification", task, "nothing is verified while the dependency waits for approval")
        task = self.eng.answer(task["id"], "Remove it")["task"]
        self.assertEqual(calls[1].session_id, task["steps"][0]["session_id"], "the answer went back to the same worker")
        self.assertIn("Remove it", calls[1].prompt)
        self.assertEqual(task["status"], "done")

    def test_an_approved_dependency_is_kept(self):
        calls = []
        self.s.plan = lambda req: plan([step()])
        self.s.step = self._dep_worker(calls)
        task = self.eng.handle("pad the totals", self.ws.ui)["task"]
        task = self.eng.answer(task["id"], "Keep it")["task"]
        self.assertEqual(task["status"], "done")
        self.assertEqual(sum(1 for q in task["questions"] if q.get("kind") == "dependency"), 1, "asked once")


class DependencyNameTest(unittest.TestCase):
    def test_names_match_as_words(self):
        from orchestrator.engine import _names
        self.assertFalse(_names("ms", "How many items per page?"))
        self.assertTrue(_names("ms", "Can we add ms for durations?"))
        self.assertTrue(_names("left-pad", "Add left-pad?"))
        self.assertFalse(_names("pad", "Add left-pad?"))


class ManifestTest(unittest.TestCase):
    def test_declared_dependencies(self):
        self.assertEqual(declared("package.json", '{"dependencies": {"a": "1"}, "devDependencies": {"b": "2"}}'),
                         {"a", "b"})
        self.assertEqual(declared("requirements.txt", "Requests>=2\n# c\n-r base.txt\nflask[async]==3\n"),
                         {"requests", "flask"})
        self.assertEqual(declared("pyproject.toml", '[project]\ndependencies = [\n  "pyyaml>=6",\n  "rich",\n]\n'),
                         {"pyyaml", "rich"})
        self.assertEqual(declared("go.mod", "module x\nrequire (\n\tgithub.com/a/b v1.0.0\n)\nrequire c.io/d v2\n"),
                         {"github.com/a/b", "c.io/d"})
        self.assertEqual(declared("Cargo.toml", "[package]\nname='x'\n[dependencies]\nserde = '1'\n"), {"serde"})
        self.assertEqual(declared("package.json", "not json"), set())


if __name__ == "__main__":
    unittest.main()


class ReviewExistingWorkTest(unittest.TestCase):
    """Workflow D: QA reviews changes Foreman didn't make; problems are reported, nothing is fixed."""

    def setUp(self):
        self.ws = Workspace()
        self.s = Script()
        self.eng = make_engine(self.ws, self.s)
        self.qa_calls = []
        self.dev_calls = []
        self.s.step = lambda req: (self.dev_calls.append(req), {"status": "done", "summary": "x"})[1]

    def tearDown(self):
        self.ws.cleanup()

    def review_plan(self, kind="uncommitted", base=None, steps=None):
        p = plan(steps or [], projects=(("admin-ui", "inspect"),))
        p["review_target"] = {"kind": kind, **({"base": base} if base else {})}
        return p

    def test_your_uncommitted_changes_are_reviewed_not_fixed(self):
        write(self.ws.ui, "src/orders.js", "export const show = (o) => o.total - o.refund;\n")
        self.s.plan = lambda req: self.review_plan()
        self.s.verify = lambda req: (self.qa_calls.append(req), qa("revise", [BUG]))[1]
        res = self.eng.handle("QA my changes to the orders page", self.ws.ui, mode="qa")
        task = res["task"]
        self.assertEqual(self.dev_calls, [])
        prompt = self.qa_calls[0].prompt
        self.assertIn("REVIEW OF EXISTING WORK", prompt)
        self.assertIn("+export const show = (o) => o.total - o.refund;", prompt)
        self.assertEqual(task["fix_rounds"], 0)
        self.assertTrue(any("total can go negative" in r for r in task["remaining"]))
        self.assertEqual(task["review"]["state"], "revise")
        self.assertIn("Reviewed", res["report"])
        self.assertIn("your uncommitted changes (1 file)", res["report"])
        self.assertEqual(task["document"]["kind"], "qa")
        self.assertEqual(task["next"]["mode"], "development")  # fixing is the next step, on your say-so
        with open(f"{self.ws.ui}/src/orders.js") as f:
            self.assertIn("o.refund", f.read(), "your change is untouched")

    def test_a_branch_is_reviewed_against_where_it_left_its_base(self):
        from helpers import git
        base = git(self.ws.ui, "rev-parse", "--abbrev-ref", "HEAD").strip()
        git(self.ws.ui, "checkout", "-qb", "feature")
        write(self.ws.ui, "src/vip.js", "module.exports = 0.1;\n")
        git(self.ws.ui, "add", "-A")
        git(self.ws.ui, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "vip")
        self.s.plan = lambda req: self.review_plan("branch", base)
        self.s.verify = lambda req: (self.qa_calls.append(req), qa())[1]
        task = self.eng.handle("review the feature branch", self.ws.ui, mode="qa")["task"]
        self.assertIn("+module.exports = 0.1;", self.qa_calls[0].prompt)
        self.assertEqual(task["review"], {"target": f"changes since {base}", "files": 1, "state": "approved",
                                          "checks_failed": []})

    def test_a_review_qa_could_not_run_is_blocked_not_achieved(self):
        write(self.ws.ui, "src/orders.js", "export const show = (o) => o.total - o.refund;\n")
        self.s.plan = lambda req: self.review_plan()
        self.s.verify = lambda req: (self.qa_calls.append(req), {"summary": "no verdict"})[1]  # malformed everywhere
        res = self.eng.handle("QA my changes to the orders page", self.ws.ui, mode="qa")
        task = res["task"]
        self.assertTrue(self.qa_calls, "QA was attempted")
        self.assertEqual(task["status"], "blocked")
        self.assertEqual(task["review"]["state"], "unavailable")
        self.assertIn("QA review could not run", task["remaining"])
        first = res["report"].splitlines()[0]
        self.assertNotIn("achieved", first)
        self.assertNotIn("✔", first)
        self.assertNotIn("document", task, "no QA report is written for a review that didn't happen")

    def test_nothing_to_review_is_said_plainly(self):
        self.s.plan = lambda req: self.review_plan()
        self.s.verify = lambda req: (self.qa_calls.append(req), qa())[1]
        task = self.eng.handle("QA my changes", self.ws.ui, mode="qa")["task"]
        self.assertEqual(self.qa_calls, [])
        self.assertTrue(any("Nothing to review" in n for n in task["notices"]))

    def test_a_review_target_with_implement_steps_is_ignored(self):
        self.s.plan = lambda req: self.review_plan(steps=[step()])
        task = self.eng.handle("fix and review", self.ws.ui)["task"]
        self.assertNotIn("review_target", task)
