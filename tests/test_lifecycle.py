"""Lifecycle modes, project knowledge in .foreman/, the impact gate and next-step recommendations."""

import os
import unittest

from helpers import Script, Workspace, git, make_engine, write
from test_scenarios import editing_worker, plan, step

from orchestrator.brain import PLAN_SCHEMA, STEP_SCHEMA
from orchestrator.chat import Chat, format_event
from orchestrator.config import ProviderSettings
from orchestrator.engine import check_command_allowed
from orchestrator.knowledge import ProjectKnowledge
from orchestrator.modes import load_modes, resolve
from orchestrator.providers.generic import GenericProvider
from orchestrator.report import render_report
from orchestrator.util import changed_since, snapshot


def with_mode(p, mode, feature="vip discount", impact=None, reason="fits the request"):
    p.update({"mode": mode, "feature": feature, "mode_reason": reason})
    if impact:
        p["impact"] = impact
    return p


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.s = Script()
        self.eng = make_engine(self.ws, self.s)
        self.prompts = []

    def tearDown(self):
        self.ws.cleanup()

    def calls(self, schema=None):
        return [c for p in self.eng.providers.values() for c in p.calls if schema is None or c.schema is schema]

    # ------------------------------------------------------------------ modes as files
    def test_builtin_modes_and_overrides(self):
        modes = load_modes()
        self.assertEqual(list(modes), ["research", "planning", "development", "qa", "deployment"])
        self.assertEqual((modes["qa"].writes, modes["research"].web, modes["deployment"].writes), ("tests", True, "none"))
        self.assertEqual(resolve("R&D", modes), "research")
        self.assertEqual(resolve("deploy", modes), "deployment")
        write(self.ws.api, ".foreman/modes/qa.md", "---\ntitle: Team QA\nwrites: none\nchecks: [lint]\n---\n"
                                                      "## Planner\nUse the team checklist.\n")
        mine = load_modes(self.eng.cfg.home, self.ws.api)
        self.assertEqual((mine["qa"].title, mine["qa"].writes, mine["qa"].source), ("Team QA", "none", ".foreman/modes"))
        self.assertIn("team checklist", mine["qa"].section("planner"))

    # ------------------------------------------------------------------ planning: read-only, plan document, lifecycle
    def test_planning_mode_reads_only_and_saves_a_plan_document(self):
        def planner(req):
            self.prompts.append(req.prompt)
            return with_mode(plan([("orders-api", "modify")], [step("1", "orders-api", text="add the VIP discount")]),
                             "planning")
        self.s.plan, self.s.step = planner, editing_worker()
        self.s.document = lambda req: {"title": "VIP discount plan", "document": "# Plan\n\n1. Add VIP flag.",
                                       "summary": "Two changes in orders-api, one consumer to update."}
        res = self.eng.handle("plan a VIP discount", self.ws.api, mode="planning")
        task = res["task"]
        self.assertIn('The user chose the "planning" mode', self.prompts[0])
        steps = self.calls(STEP_SCHEMA)
        self.assertTrue(steps and all(c.mode == "read" for c in steps), "planning never edits code")
        self.assertTrue(all(c.web for c in steps), "planning may research the web")
        doc = os.path.join(self.ws.api, ".foreman", "plans", "vip-discount.md")
        self.assertTrue(os.path.isfile(doc))
        self.assertIn("Add VIP flag", open(doc).read())
        self.assertEqual(task["document"]["path"], ".foreman/plans/vip-discount.md")
        self.assertEqual(task["projects"]["orders-api"]["role"], "inspect", "nothing is 'to change' in planning")
        self.assertEqual(git(self.ws.api, "status", "--porcelain", "--", "server.js"), "", "no code touched")
        self.assertFalse([n for n in task["notices"] if "changed files" in n], ".foreman writes are not changes")
        life = ProjectKnowledge(self.ws.api).lifecycle()["vip-discount"]
        self.assertEqual((life["stage"], life["next"]), ("planning", "development"))
        self.assertIn("Document", res["report"])
        self.assertIn("Next step", res["report"])
        self.assertIn("Development: implement it", res["report"])

    def test_research_steps_use_the_web_development_steps_do_not(self):
        self.s.plan = lambda req: with_mode(plan([("orders-api", "inspect")], [step("1", "orders-api", kind="investigate")]),
                                            "research")
        self.eng.handle("research how competitors price VIP tiers", self.ws.api)
        self.assertTrue(self.calls(STEP_SCHEMA)[-1].web)
        self.assertTrue(os.path.isdir(os.path.join(self.ws.api, ".foreman", "research")))
        self.s.plan = lambda req: with_mode(plan([("admin-ui", "modify")], [step("2", "admin-ui")]), "development")
        self.s.step = editing_worker()
        self.eng.handle("add the badge", self.ws.ui)
        self.assertFalse(self.calls(STEP_SCHEMA)[-1].web)

    def test_a_new_empty_folder_is_the_home_of_its_own_work(self):
        """A folder created after the last scan, with no git or manifest yet, is still the project the work belongs to."""
        self.eng.registry.scan()
        fresh = os.path.join(self.ws.root, "fresh-dataset")
        os.makedirs(fresh)
        self.assertIsNone(self.eng.registry.project_for_path(fresh))
        self.s.plan = lambda req: with_mode(plan([("orders-api", "inspect")], [step("1", "orders-api", kind="investigate")]),
                                            "research")
        task = self.eng.handle("research the orders service for a dataset", fresh)["task"]
        proj = self.eng.registry.project_for_path(fresh)
        self.assertEqual((proj.name, proj.source), ("fresh-dataset", "declared"))
        self.assertTrue(os.path.isdir(os.path.join(fresh, ".foreman", "research")), "the document is written in the new folder")
        self.assertEqual(task["document"]["project"], "fresh-dataset")
        self.assertFalse(os.path.exists(os.path.join(self.ws.api, ".foreman", "research")), "not in the project it only read")
        self.eng.registry.scan()
        self.assertIn("fresh-dataset", self.eng.registry.projects, "a rescan keeps it")
        self.assertIsNone(self.eng.registry.adopt("/tmp"), "folders outside the project roots are not adopted")

    # ------------------------------------------------------------------ auto mode + next step
    def test_recommended_mode_then_next_starts_qa(self):
        self.s.plan = lambda req: with_mode(plan([("admin-ui", "modify")], [step("1", "admin-ui")]), "development",
                                            feature="vip badge", reason="small, clear UI change")
        self.s.step = editing_worker()
        res = self.eng.handle("show a VIP badge on the orders page", self.ws.ui)
        task = res["task"]
        self.assertEqual((task["mode"], task["mode_source"]), ("development", "auto"))
        self.assertIn("Development (recommended: small, clear UI change)", res["report"])
        self.assertEqual(task["next"]["mode"], "qa")
        plans_before = len(self.calls(PLAN_SCHEMA))
        state, msg = self.eng.route("what's next?", self.ws.ui)
        self.assertIsNone(state)
        self.assertIn("Recommended: QA for vip-badge", msg)
        self.assertEqual(len(self.calls(PLAN_SCHEMA)), plans_before, "lifecycle answers need no AI")
        nxt, _ = self.eng.route("next", self.ws.ui)
        self.assertEqual((nxt["mode"], nxt["mode_source"], nxt["parent"], nxt["feature"]),
                         ("qa", "user", task["id"], "vip-badge"))
        self.assertEqual(self.eng.store.load(task["id"])["next_started"], nxt["id"])

    def test_a_finished_planning_step_moves_on_even_with_open_items_but_a_failed_one_repeats(self):
        def planner(req):
            return with_mode(plan([("orders-api", "inspect")], [step("1", "orders-api", kind="investigate")]), "planning",
                             feature="vip discount")
        self.s.plan = planner
        self.s.step = lambda req: {"status": "done", "summary": "planned", "findings": [],
                                   "questions": [], "dependencies": []}
        self.s.document = lambda req: {"title": "Plan", "document": "# Plan", "summary": "Done."}
        done = self.eng.handle("plan vip discount", self.ws.api, mode="planning")["task"]
        done["remaining"] = ["Target project is not a git repo", "No contact address"]
        nxt = self.eng._next_step(done, load_modes()["planning"])
        self.assertEqual(nxt["mode"], "development")
        self.assertIn("settle the 2 open items first", nxt["reason"])
        done["steps"][0]["status"], done["status"] = "failed", "blocked"
        nxt = self.eng._next_step(done, load_modes()["planning"])
        self.assertEqual((nxt["mode"], nxt["resume"]), ("planning", done["id"]), "the same task carries on")
        self.assertIn("1 step left", nxt["reason"])

    def test_go_for_plan_after_research_starts_the_planning_step(self):
        from orchestrator.engine import NEXT_DO_RE
        self.s.plan = lambda req: with_mode(plan([("orders-api", "inspect")], [step("1", "orders-api", kind="investigate")]),
                                            "research", feature="dataset")
        done = self.eng.handle("research the dataset options", self.ws.api)["task"]
        self.assertEqual(done["next"]["mode"], "planning")
        nxt, _ = self.eng.route("go for plan", self.ws.api)
        self.assertEqual((nxt["mode"], nxt["parent"]), ("planning", done["id"]))
        for phrase in ("go for planning", "Start planning", "let's go", "do the plan", "go for the next step", "go for QA mode"):
            self.assertTrue(NEXT_DO_RE.match(phrase), phrase)
        for phrase in ("go for a walk", "go for plan B and explain it", "start the server"):
            self.assertFalse(NEXT_DO_RE.match(phrase), phrase)

    def test_problems_found_in_the_qa_report_send_it_back_to_development(self):
        self.s.plan = lambda req: with_mode(plan([("admin-ui", "test")], [step("1", "admin-ui", kind="investigate")]), "qa")
        self.s.document = lambda req: {"title": "QA", "document": "# QA\n", "summary": "One defect.",
                                       "open_problems": ["non-VIP totals are not rounded to cents (pricing.js:3)"]}
        task = self.eng.handle("QA the badge", self.ws.ui, mode="qa")["task"]
        self.assertIn("non-VIP totals are not rounded to cents (pricing.js:3)", task["remaining"])
        self.assertEqual(task["next"]["mode"], "development")

    def test_test_files_are_recognised(self):
        from orchestrator.engine import TEST_PATH_RE
        for f in ("test.js", "tests/a.py", "src/a.test.ts", "a_test.go", "test_x.py", "tests.php", "UserTest.php",
                  "e2e/login.spec.ts"):
            self.assertTrue(TEST_PATH_RE.search(f), f)
        for f in ("src/orders.js", "pricing.js", "latest.js", "contest/app.js"):
            self.assertFalse(TEST_PATH_RE.search(f), f)

    def test_problems_send_qa_back_to_development(self):
        write(self.ws.ui, ".foreman/checks.yaml", "lint: node -e \"process.exit(3)\"\n")
        self.s.plan = lambda req: with_mode(plan([("admin-ui", "test")], [step("1", "admin-ui", kind="investigate")]), "qa")
        res = self.eng.handle("QA the badge", self.ws.ui, mode="qa")
        task = res["task"]
        lint = [e for e in task["evidence"] if e["kind"] == "check" and "process.exit(3)" in e["command"]]
        self.assertTrue(lint and not lint[0]["ok"], "QA runs the project's own lint check")
        self.assertEqual(task["next"], {"mode": "development", "reason": "fix what QA found", "title": "Development"})
        self.assertTrue(os.path.isdir(os.path.join(self.ws.ui, ".foreman", "qa")), "QA report saved")

    # ------------------------------------------------------------------ QA writes tests only, never fixes app code
    def test_qa_flags_application_changes_and_does_not_run_fix_rounds(self):
        self.s.plan = lambda req: with_mode(plan([("admin-ui", "modify")], [step("1", "admin-ui", text="add tests")]), "qa")

        def worker(req):
            if req.mode == "write":
                write(req.cwd, "src/orders.js", "export const show = () => 0;\n")
                write(req.cwd, "tests/orders.test.js", "// new test\n")
            return {"status": "done", "summary": "added tests"}
        self.s.step = worker
        self.s.review = lambda req: {"verdict": "revise", "summary": "bug", "issues": [
            {"project": "admin-ui", "problem": "total is ignored", "fix": "use o.total"}]}
        task = self.eng.handle("QA the orders page", self.ws.ui, mode="qa")["task"]
        self.assertIn("QA may only change tests", " ".join(task["notices"]))
        self.assertIn("src/orders.js", " ".join(task["notices"]))
        self.assertNotIn("tests/orders.test.js", " ".join(n for n in task["notices"] if "QA rule" in n))
        self.assertEqual(task["fix_rounds"], 0, "QA reports problems instead of fixing them")
        self.assertTrue(any("total is ignored" in r for r in task["remaining"]))
        self.assertIn("QA rule", " ".join(task["notices"]))

    # ------------------------------------------------------------------ impact gate
    def test_consumer_left_out_gets_an_impact_check_before_editing(self):
        order = []

        def worker(req):
            order.append((req.mode, os.path.basename(req.cwd)))
            return editing_worker()(req)
        self.s.plan = lambda req: with_mode(plan([("orders-api", "modify")], [step("1", "orders-api")]), "development",
                                            impact={"summary": "renames total", "risk": "low"})
        self.s.step = worker
        task = self.eng.handle("rename total to amount in the orders API", self.ws.api)["task"]
        impact = [s for s in task["steps"] if s.get("impact")]
        self.assertEqual(len(impact), 1)
        self.assertIn("admin-ui", impact[0]["read_projects"], "the UI consumes the API")
        self.assertEqual(task["projects"]["admin-ui"]["role"], "inspect")
        self.assertEqual(order[0], ("read", "orders-api"), "impact analysis runs before any edit")
        self.assertEqual(order[1][0], "write")

    def test_risky_change_gets_impact_analysis_even_without_consumers(self):
        self.s.plan = lambda req: with_mode(plan([("admin-ui", "modify")], [step("1", "admin-ui")]), "development",
                                            impact={"summary": "changes refund amounts", "risk": "high"})
        self.s.step = editing_worker()
        task = self.eng.handle("change how refunds are rounded", self.ws.ui)["task"]
        self.assertTrue(any(s.get("impact") for s in task["steps"]))
        self.assertIn("Impact    high risk: changes refund amounts", render_report(task))

    def test_plain_question_has_no_mode_and_no_lifecycle(self):
        self.s.plan = lambda req: with_mode(plan([], [], intent="answer", answer="In pricing.js."), "none")
        task = self.eng.handle("where is the discount applied?", self.ws.api)["task"]
        self.assertIsNone(task.get("mode"))
        self.assertFalse(os.path.exists(os.path.join(self.ws.api, ".foreman")), "questions leave no trace in the repo")

    def test_answering_a_planning_question_does_not_start_development(self):
        q = [{"text": "What is the VIP rate?", "kind": "business"}]
        self.s.plan = lambda req: with_mode(plan([("orders-api", "modify")], [step("1", "orders-api")], questions=q),
                                            "planning")
        task = self.eng.handle("add a VIP discount", self.ws.api)["task"]
        self.s.plan = lambda req: with_mode(plan([("orders-api", "modify")], [step("1", "orders-api")]), "development")
        self.s.step = editing_worker()
        done = self.eng.answer(task["id"], "5%")["task"]
        self.assertEqual(done["mode"], "planning")
        self.assertFalse([c for c in self.calls(STEP_SCHEMA) if c.mode == "write"], "no code was changed")
        self.assertEqual(done["next"]["mode"], "development", "development is the next step, the user's call")

    # ------------------------------------------------------------------ decisions outlive tasks
    def test_answered_business_question_is_recorded_and_followed_later(self):
        q = [{"text": "Which customers are VIP?", "kind": "business"}]
        self.s.plan = lambda req: with_mode(plan([("orders-api", "modify")], [step("1", "orders-api")], questions=q),
                                            "development")
        task = self.eng.handle("add VIP pricing", self.ws.api)["task"]
        self.assertEqual(task["status"], "needs_input")
        self.s.step = editing_worker()
        self.s.plan = lambda req: with_mode(plan([("orders-api", "modify")], [step("1", "orders-api")]), "development")
        self.eng.answer(task["id"], "lifetime spend above $5,000")
        log = open(os.path.join(self.ws.api, ".foreman", "decisions.md")).read()
        self.assertIn("Which customers are VIP? → lifetime spend above $5,000", log)
        self.assertEqual(self.eng.store.load(task["id"])["mode"], "development", "the mode stays with the task")

        seen = []
        self.s.plan = lambda req: (seen.append(req.prompt), with_mode(plan([("orders-api", "modify")],
                                                                          [step("2", "orders-api")]), "development"))[1]
        self.eng.handle("show VIP status in the API response", self.ws.api)
        self.assertIn("Confirmed business decisions", seen[0])
        self.assertIn("lifetime spend above $5,000", seen[0])
        step_prompt = [c for c in self.calls(STEP_SCHEMA) if c.mode == "write"][-1].prompt
        self.assertIn("lifetime spend above $5,000", step_prompt, "workers get the decision too")


class SafetyAndProvidersTest(unittest.TestCase):
    def test_deployment_commands_are_never_run_as_checks(self):
        for cmd in ("npm run deploy", "php artisan migrate", "php artisan migrate --force", "npm run release",
                    "php artisan db:seed", "npx prisma db push"):
            self.assertFalse(check_command_allowed(cmd)[0], cmd)
        for cmd in ("php artisan migrate:status", "npm run build", "npm run lint"):
            self.assertTrue(check_command_allowed(cmd)[0], cmd)

    def test_display_flag_before_a_subcommand(self):
        from unittest import mock
        from orchestrator import cli
        with mock.patch.object(cli, "_engine") as eng, mock.patch.object(cli, "_print_result"):
            cli.main(["--headless", "answer", "4", "yes"])
        eng.return_value.answer.assert_called_once_with("4", "yes")

    def test_foreman_folder_is_not_a_change(self):
        ws = Workspace()
        try:
            before = snapshot(ws.api)
            ProjectKnowledge(ws.api).add_decision("Q?", "A", "1")
            self.assertEqual(changed_since(ws.api, before), [])
            write(ws.api, "server.js", "changed")
            self.assertEqual(changed_since(ws.api, before), ["server.js"])
        finally:
            ws.cleanup()

    def test_generic_provider_from_config(self):
        settings = ProviderSettings(name="codex", command="codex", extra={
            "type": "generic", "read_args": ["exec", "--sandbox", "read-only", "{prompt}"], "read_only_enforced": True})
        prov = GenericProvider(settings)
        from orchestrator.providers.base import RunRequest
        cmd = prov.build_command(RunRequest(prompt="look", cwd="/tmp", schema=STEP_SCHEMA))
        self.assertEqual(cmd[1:4], ["exec", "--sandbox", "read-only"])
        self.assertIn("look", cmd[4])
        self.assertIn("```json", cmd[4], "asks for a fenced JSON block")
        self.assertTrue(prov.capabilities.enforced_read_only)
        self.assertEqual(prov.name, "codex")


class ChatModeTest(unittest.TestCase):
    def test_mode_command_applies_to_next_requests(self):
        ws, s = Workspace(), Script()
        try:
            eng = make_engine(ws, s)

            class InProcess(Chat):
                def _execute(self, task):
                    self.eng.execute(task)
            chat = InProcess(ws.api, engine=eng)
            self.assertIn("Mode: QA", chat.respond("/mode qa"))
            self.assertIn("QA", chat._status_line(), "the status line shows the mode")
            s.plan = lambda req: plan([("orders-api", "test")], [step("1", "orders-api", kind="investigate")])
            chat.respond("check the discount rules")
            task = eng.store.load(chat.focus)
            self.assertEqual((task["mode"], task["mode_source"]), ("qa", "user"))
            self.assertIn("Mode: auto", chat.respond("/mode auto"))
            self.assertIn("Lifecycle of orders-api", chat.respond("/lifecycle"))
            line = format_event({"event": "mode", "mode": "qa", "title": "QA", "source": "user"})
            self.assertIn("Mode(QA)", ui_plain(line))
        finally:
            ws.cleanup()


def ui_plain(s):
    from orchestrator.ui import ANSI
    return ANSI.sub("", s)


if __name__ == "__main__":
    unittest.main()
