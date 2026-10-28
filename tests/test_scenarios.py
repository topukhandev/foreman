"""End-to-end orchestration scenarios with scripted providers and real git repos, checks and state."""

import os
import time
import unittest

from helpers import Script, Workspace, git, make_engine, write

from orchestrator.providers.base import SessionInfo


def plan(projects, steps, checks=None, questions=None, intent="change", answer=None, title="Task", suggestions=None):
    return {"intent": intent, "title": title, "answer": answer or "",
            "projects": [{"name": n, "role": r, "why": "needed"} for n, r in projects],
            "steps": steps, "checks": checks or [], "questions": questions or [], "suggestions": suggestions or []}


def step(id_, project, kind="implement", deps=None, reads=None, text="do the work"):
    return {"id": id_, "project": project, "kind": kind, "instructions": text, "depends_on": deps or [],
            "read_projects": reads or []}


def editing_worker(content="// changed\n"):
    """Worker that edits a file in whatever project it runs in."""
    def run(req):
        if req.mode == "write":
            write(req.cwd, "src/change.js", content)
        return {"status": "done", "summary": f"edited {os.path.basename(req.cwd)}", "files_changed": ["src/change.js"]}
    return run


class ScenarioTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.s = Script()
        self.notes = []
        self.eng = make_engine(self.ws, self.s, notes=self.notes)

    def tearDown(self):
        self.ws.cleanup()

    def calls(self):
        return sum(len(p.calls) for p in self.eng.providers.values())

    # 1 ---------------------------------------------------------------------------------------
    def test_simple_single_project_task(self):
        seen = {}

        def p(req):
            seen["prompt"] = req.prompt
            return plan([("admin-ui", "modify")], [step("1", "admin-ui")])
        self.s.plan, self.s.step = p, editing_worker()
        res = self.eng.handle("Fix the total formatting on the orders page", self.ws.ui)
        task = res["task"]
        self.assertEqual(task["status"], "done")
        # Context economy: unrelated project appears only as a name, related neighbour as a one-line card.
        self.assertIn("[current directory]", seen["prompt"])
        self.assertIn("Other known projects (no known relation): e2e-tests, notes-app", seen["prompt"])
        self.assertNotIn("personal notes", seen["prompt"])
        # Worker only touched admin-ui; Foreman ran the registry test command itself.
        self.assertEqual([l for l in self.s.log if ":step:" in l], ["claude:step:admin-ui"])
        checks = [e for e in task["evidence"] if e["kind"] == "check"]
        self.assertEqual([(c["project"], c["command"], c["ok"]) for c in checks], [("admin-ui", "npm test", True)])
        self.assertEqual(task["projects"]["admin-ui"]["changed_files"], ["src/change.js"])
        # Independent review by a different provider than the implementer.
        self.assertIn("opencode:verify", self.s.log)
        self.assertLess(len(res["report"].splitlines()), 20)
        self.assertEqual(len(self.notes), 1, "exactly one notification, at the end")

    # 2 + 3 -----------------------------------------------------------------------------------
    def test_frontend_backend_and_integration_tests(self):
        order = []

        def worker(req):
            order.append(os.path.basename(req.cwd))
            return editing_worker()(req)
        self.s.plan = lambda req: plan(
            [("orders-api", "modify"), ("admin-ui", "modify"), ("e2e-tests", "test")],
            [step("api", "orders-api", text="rename total->amount"),
             step("ui", "admin-ui", deps=["api"], reads=["orders-api"], text="use amount")],
            checks=[{"project": "e2e-tests", "command": "npm test", "why": "integration"}])
        self.s.step = worker
        task = self.eng.handle("Rename total to amount in the orders API and admin panel, keep e2e green", self.ws.ui)["task"]
        self.assertEqual(order, ["orders-api", "admin-ui"], "dependency order respected")
        self.assertEqual(task["status"], "done")
        ran = {(e["project"], e["ok"]) for e in task["evidence"] if e["kind"] == "check"}
        self.assertEqual(ran, {("e2e-tests", True), ("orders-api", True), ("admin-ui", True)})
        ui_step = next(s for s in task["steps"] if s["project"] == "admin-ui")
        self.assertEqual(ui_step["read_projects"], ["orders-api"])
        ui_call = [c for c in self.eng.providers["claude"].calls if c.cwd == self.ws.ui and c.mode == "write"][0]
        self.assertIn(self.ws.api, ui_call.extra_dirs)

    # 4 ---------------------------------------------------------------------------------------
    def test_status_query_uses_no_ai_and_detects_staleness(self):
        self.s.plan = lambda req: plan([("admin-ui", "modify")], [step("1", "admin-ui")], title="Orders page totals")
        self.s.step = editing_worker()
        self.eng.handle("Fix the orders page totals", self.ws.ui)
        before = self.calls()
        rep = self.eng.handle("What is the current state of the task I was working on?", self.ws.ui)["report"]
        self.assertEqual(self.calls(), before, "status must not call any provider")
        self.assertIn("Orders page totals", rep)
        self.assertNotIn("changed since", rep)
        write(self.ws.ui, "src/orders.js", "export const show = (o) => o.amount;\n")
        rep = self.eng.handle("what's the status of the orders page task?", self.ws.ui)["report"]
        self.assertIn("admin-ui changed since this task last ran", rep)

    def test_project_state_question_goes_to_planner_not_task_status(self):
        self.s.plan = lambda req: plan([("admin-ui", "inspect")], [], intent="answer", answer="Clean tree, 1 commit.")
        res = self.eng.handle("whats current state of this project?", self.ws.ui)
        self.assertEqual(res["task"]["status"], "answered")
        self.assertEqual(self.s.log, ["claude:plan"])

    # 5 ---------------------------------------------------------------------------------------
    def test_resume_incomplete_work(self):
        self.s.plan = lambda req: plan([("orders-api", "modify"), ("e2e-tests", "modify")],
                                       [step("api", "orders-api"), step("it", "e2e-tests", deps=["api"])],
                                       title="Refund endpoint with integration coverage")
        self.s.step = editing_worker()
        self.s.fail_steps = lambda prov, req: os.path.basename(req.cwd) == "e2e-tests"
        task = self.eng.handle("Add a refund endpoint and integration scenario", self.ws.api)["task"]
        self.assertEqual(task["status"], "blocked")
        self.assertTrue(any("e2e-tests" in r for r in task["remaining"]))

        prompts = []

        def replan(req):
            prompts.append(req.prompt)
            return plan([("e2e-tests", "modify")], [step("it2", "e2e-tests")])
        self.s.plan, self.s.fail_steps = replan, False
        res = self.eng.handle("Continue the previous task and finish the remaining integration work", self.ws.ui)
        self.assertEqual(res["task"]["id"], task["id"], "resolved the previous task without being told which")
        self.assertIn("TASK BEING CONTINUED", prompts[0])
        self.assertIn("[done] implement in orders-api", prompts[0])
        self.assertEqual(res["task"]["status"], "done")
        api_steps = [s for s in res["task"]["steps"] if s["project"] == "orders-api" and s["kind"] == "implement"]
        self.assertEqual(len(api_steps), 1, "completed work is not redone")

    # 6 ---------------------------------------------------------------------------------------
    def test_independent_steps_run_in_parallel_sessions(self):
        self.s.plan = lambda req: plan([("orders-api", "modify"), ("admin-ui", "modify")],
                                       [step("a", "orders-api"), step("b", "admin-ui")])
        self.s.step, self.s.delay = editing_worker(), 0.4
        task = self.eng.handle("Add request logging to the orders API and the admin UI", self.ws.ui)["task"]
        self.assertEqual(self.eng.providers["claude"].max_active, 2, "both steps ran at the same time")
        self.assertEqual(len({s["session_id"] for s in task["sessions"] if not s.get("reviewer")}), 2)

    # 7 ---------------------------------------------------------------------------------------
    def test_reuses_existing_session_by_forking_and_continues_it_for_fixes(self):
        user_session = SessionInfo("claude", "user-sess-1", self.ws.ui, time.time() - 600, "orders page work")
        self.eng = make_engine(self.ws, self.s, sessions={"claude": [user_session]}, notes=self.notes)
        self.s.plan = lambda req: plan([("admin-ui", "modify")], [step("1", "admin-ui")])
        self.s.step = editing_worker()
        reviews = iter([{"verdict": "revise", "summary": "bug", "issues": [{"project": "admin-ui", "problem": "null total", "fix": "guard"}]},
                        {"verdict": "approve", "summary": "fixed", "issues": []}])
        self.s.review = lambda req: next(reviews)
        task = self.eng.handle("Show totals with currency", self.ws.ui)["task"]
        calls = [c for c in self.eng.providers["claude"].calls if c.schema and "status" in c.schema["properties"]]
        self.assertEqual((calls[0].session_id, calls[0].fork), ("user-sess-1", True), "forked the user's recent session")
        self.assertEqual(calls[1].fork, False)
        self.assertEqual(calls[1].session_id, task["steps"][0]["session_id"], "fix continued the task's own session")
        self.assertEqual(task["fix_rounds"], 1)
        self.assertEqual(task["status"], "done")
        self.assertTrue(task["sessions"][0]["forked_from"] == "user-sess-1")

    def test_never_forks_foreman_own_sessions(self):
        own = SessionInfo("claude", "brain-1", self.ws.ui, time.time() - 60,
                          "You are the planning layer of Foreman, an orchestrator that coordinates")
        self.eng = make_engine(self.ws, self.s, sessions={"claude": [own]})
        self.s.plan = lambda req: plan([("admin-ui", "modify")], [step("1", "admin-ui")])
        self.s.step = editing_worker()
        self.eng.handle("Add a footer", self.ws.ui)
        call = [c for c in self.eng.providers["claude"].calls if c.mode == "write"][0]
        self.assertIsNone(call.session_id)
        brain_calls = [c for c in self.eng.providers["claude"].calls if c.schema and "intent" in c.schema["properties"]]
        self.assertFalse(brain_calls[0].persist, "planning sessions are not persisted")

    def test_interrupted_run_is_detected_and_resumed(self):
        self.s.plan = lambda req: plan([("admin-ui", "modify")], [step("1", "admin-ui")], title="Footer work")
        self.s.step = editing_worker()
        task = self.eng.store.new_task("Add a footer", self.ws.ui)
        task.update(status="running", steps=[{"id": "x", "project": "admin-ui", "kind": "implement", "instructions": "footer",
                                              "depends_on": [], "read_projects": [], "status": "running", "attempts": 1,
                                              "provider": None, "session_id": None, "summary": ""}],
                    projects={"admin-ui": {"path": self.ws.ui, "role": "modify", "why": ""}})
        self.eng.store.save(task)
        with open(self.eng.store.path(task["id"], "lock"), "w") as f:
            f.write("999999")  # dead pid
        self.assertIn("interrupted", self.eng.handle("status", self.ws.ui)["report"])
        res = self.eng.handle("continue the previous task", self.ws.ui)
        self.assertEqual(res["task"]["id"], task["id"])
        self.assertEqual(res["task"]["status"], "done")

    def test_does_not_fork_stale_or_unsupported_sessions(self):
        old = SessionInfo("claude", "old", self.ws.ui, time.time() - 10 * 24 * 3600, "old work")
        self.eng = make_engine(self.ws, self.s, sessions={"claude": [old]})
        self.s.plan = lambda req: plan([("admin-ui", "inspect")], [step("1", "admin-ui", kind="investigate")], intent="answer")
        self.eng.handle("Where is the orders total rendered?", self.ws.ui)
        call = [c for c in self.eng.providers["claude"].calls if c.schema and "findings" in c.schema["properties"]][0]
        self.assertIsNone(call.session_id)
        self.assertEqual(call.mode, "read")

    # 8 ---------------------------------------------------------------------------------------
    def test_unmentioned_dependency_reaches_planner_and_discoveries_expand_plan(self):
        seen = []

        def p(req):
            seen.append(req.prompt)
            if "Workers discovered" in req.prompt:
                return plan([("e2e-tests", "modify")], [step("e2e", "e2e-tests", text="update scenarios to amount")])
            return plan([("orders-api", "modify")], [step("api", "orders-api")])

        def worker(req):
            out = editing_worker()(req)
            if os.path.basename(req.cwd) == "orders-api":
                out["dependencies"] = [{"project": "e2e-tests", "type": "tests", "evidence": "scenarios assert total field"}]
            return out
        self.s.plan, self.s.step = p, worker
        task = self.eng.handle("Rename the total field to amount in the orders response", self.ws.api)["task"]
        # admin-ui was never mentioned but its README links the API: planner sees it as related.
        self.assertIn("Related projects", seen[0])
        self.assertIn("- admin-ui:", seen[0])
        # Worker discovered e2e-tests -> relation learned, plan expanded, step executed.
        self.assertEqual(task["replans"], 1)
        self.assertIn("e2e-tests", task["projects"])
        rel = [r for r in self.eng.registry.relations if r.src == "orders-api" and r.dst == "e2e-tests"][0]
        self.assertEqual((rel.source, rel.type, rel.task), ("learned", "tests", task["id"]))
        self.assertEqual(task["status"], "done")

    # 9 ---------------------------------------------------------------------------------------
    def test_business_decision_pauses_then_continues_with_answer(self):
        prompts = []

        def p(req):
            prompts.append(req.prompt)
            if len(prompts) == 1:
                return plan([("orders-api", "modify")], [step("1", "orders-api")],
                            questions=[{"text": "Should refunds include shipping fees?", "kind": "business",
                                        "options": ["yes", "no"]}])
            return plan([("orders-api", "modify")], [step("1", "orders-api", text="refund excludes shipping")])
        self.s.plan, self.s.step = p, editing_worker()
        res = self.eng.handle("Add partial refunds to orders", self.ws.api)
        self.assertEqual(res["task"]["status"], "needs_input")
        self.assertIn("Should refunds include shipping fees?", res["report"])
        self.assertFalse([l for l in self.s.log if ":step:" in l], "no work before the decision")
        res = self.eng.answer(res["task"]["id"], "No, exclude shipping")
        worker_prompt = [c.prompt for c in self.eng.providers["claude"].calls if c.mode == "write"][0]
        self.assertIn("A: No, exclude shipping", worker_prompt)
        self.assertIn("exclude shipping", prompts[1])
        self.assertEqual(res["task"]["status"], "done")

    # 10 --------------------------------------------------------------------------------------
    def test_provider_failure_falls_back_to_another_provider(self):
        self.s.plan = lambda req: plan([("admin-ui", "modify")], [step("1", "admin-ui")])
        self.s.step = editing_worker()
        self.s.fail_steps = lambda prov, req: prov == "claude"
        res = self.eng.handle("Add a loading spinner", self.ws.ui)
        task = res["task"]
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["steps"][0]["provider"], "opencode")
        self.assertIn("claude failed on admin-ui", res["report"])
        # Review must then come from a provider other than the implementer (opencode) -> claude.
        self.assertIn("claude:verify", self.s.log)

    def test_all_providers_failing_blocks_with_clear_report(self):
        self.s.plan = lambda req: plan([("admin-ui", "modify")], [step("1", "admin-ui")])
        self.s.fail_steps = True
        res = self.eng.handle("Add a loading spinner", self.ws.ui)
        self.assertEqual(res["task"]["status"], "blocked")
        self.assertIn("Remaining", res["report"])
        self.assertIn("no provider could complete this step", res["report"])
        self.assertEqual(len(self.notes), 1)

    # 11 --------------------------------------------------------------------------------------
    def test_failing_check_triggers_fix_round_then_passes(self):
        self.s.plan = lambda req: plan([("orders-api", "modify")], [step("1", "orders-api")])
        attempts = []

        def worker(req):
            if req.mode != "write":  # the impact investigation (orders-api has consumers) only reads
                return {"status": "done", "summary": "consumers read id and total only"}
            attempts.append(req.prompt)
            if len(attempts) == 1:
                write(req.cwd, "test.js", "process.exit(1)\n")  # breaks the suite
            else:
                self.assertIn("Validation `npm test` fails", req.prompt)
                write(req.cwd, "test.js", "console.log('ok')\n")
            return {"status": "done", "summary": "ok"}
        self.s.step = worker
        task = self.eng.handle("Refactor the orders handler", self.ws.api)["task"]
        results = [e["ok"] for e in task["evidence"] if e["kind"] == "check"]
        self.assertEqual(results, [False, True])
        self.assertEqual(task["fix_rounds"], 1)
        self.assertEqual(task["status"], "done")

    def test_fix_rounds_are_bounded_and_escalated(self):
        self.s.plan = lambda req: plan([("orders-api", "modify")], [step("1", "orders-api")])

        def worker(req):
            write(req.cwd, "test.js", "process.exit(1)\n")
            return {"status": "done", "summary": "tried"}
        self.s.step = worker
        res = self.eng.handle("Refactor the orders handler", self.ws.api)
        self.assertEqual(res["task"]["status"], "partial")
        self.assertEqual(res["task"]["fix_rounds"], 2)
        self.assertIn("still failing", res["report"])

    # 12 --------------------------------------------------------------------------------------
    def test_answer_from_planner_needs_no_worker(self):
        self.s.plan = lambda req: plan([("admin-ui", "inspect")], [], intent="answer",
                                       answer="Totals are rendered by show() in src/orders.js.")
        res = self.eng.handle("How does admin-ui render totals?", self.ws.root)
        self.assertEqual(res["task"]["status"], "answered")
        self.assertIn("src/orders.js", res["report"])
        self.assertEqual(self.s.log, ["claude:plan"])

    # Safety -----------------------------------------------------------------------------------
    def test_worker_commit_is_flagged_and_unsafe_checks_are_skipped(self):
        self.s.plan = lambda req: plan([("admin-ui", "modify")], [step("1", "admin-ui")],
                                       checks=[{"project": "admin-ui", "command": "rm -rf node_modules && npm test"}])

        def worker(req):
            write(req.cwd, "src/x.js", "1\n")
            git(req.cwd, "add", "-A")
            git(req.cwd, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "sneaky")
            return {"status": "done", "summary": "committed"}
        self.s.step = worker
        res = self.eng.handle("Add x", self.ws.ui)
        self.assertIn("Policy violation", res["report"])
        self.assertIn("Skipped check `rm -rf node_modules && npm test`", res["report"])
        self.assertEqual(res["task"]["projects"]["admin-ui"]["changed_files"], ["src/x.js"])

    def test_read_only_step_that_edits_is_flagged(self):
        self.s.plan = lambda req: plan([("admin-ui", "inspect")], [step("1", "admin-ui", kind="investigate")], intent="answer")
        self.s.step = lambda req: (write(req.cwd, "oops.txt", "x"), {"status": "done", "summary": "found it"})[1]
        res = self.eng.handle("Where is the total rendered?", self.ws.ui)
        self.assertIn("Read-only step changed files in admin-ui: oops.txt", res["report"])

    def test_budget_warning_requires_sign_off(self):
        self.eng.budget.limits["claude"] = 10
        for _ in range(8):
            self.eng.budget.acquire("claude")
        self.s.plan = lambda req: plan([("admin-ui", "modify")], [step("1", "admin-ui")])
        self.s.step = editing_worker()
        res = self.eng.handle("Add a footer", self.ws.ui)
        self.assertEqual(res["task"]["status"], "needs_input")
        self.assertIn("% of its daily run limit", res["report"])
        res = self.eng.answer(res["task"]["id"], "yes")
        self.assertEqual(res["task"]["status"], "done")

    # Suggestions ------------------------------------------------------------------------------
    def test_suggestions_are_capped_deduplicated_and_dismissable(self):
        sugg = [{"text": "Add a DB migration for the amount column", "why": "schema changed", "project": "orders-api"},
                {"text": "Update the API docs", "why": "contract changed"},
                {"text": "Add rate limiting", "why": "x"}, {"text": "Rotate keys", "why": "y"},
                {"text": "Add an Infinity test", "why": "nit", "impact": "minor"}]
        self.s.plan = lambda req: plan([("orders-api", "modify")], [step("1", "orders-api")], suggestions=sugg)
        self.s.step = editing_worker()
        res = self.eng.handle("Rename total to amount", self.ws.api)
        self.assertEqual(len(res["task"]["suggestions"]), 3)
        self.assertIn("Worth considering (your call)", res["report"])
        self.eng.store.set_suggestion("s1", "dismissed")
        res2 = self.eng.handle("Rename total to amount again", self.ws.api)
        self.assertEqual(res2["task"]["suggestions"], ["s4"], "previously shown/dismissed items are not repeated")


if __name__ == "__main__":
    unittest.main()
