"""Chat front end: plain-language routing, plan approval, answers, suggestions, conversation context."""

import unittest

from helpers import Script, Workspace, make_engine
from test_scenarios import editing_worker, plan, step

from orchestrator.chat import Chat, format_event
from orchestrator.engine import Interrupted


class InProcessChat(Chat):
    def _execute(self, task):
        self.eng.execute(task)


class ChatTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.s = Script()
        self.notes = []
        self.eng = make_engine(self.ws, self.s, notes=self.notes)
        self.chat = InProcessChat(self.ws.ui, engine=self.eng)
        self.prompts = []

    def tearDown(self):
        self.ws.cleanup()

    def _cross_project_plan(self, req):
        self.prompts.append(req.prompt)
        return plan([("orders-api", "modify"), ("admin-ui", "modify")],
                    [step("api", "orders-api", text="add currency"), step("ui", "admin-ui", deps=["api"], text="show currency")])

    def _watch_with_keys(self, presses):
        """Run Chat._watch against a long-running process while 'typing' the given keys; returns the process."""
        import subprocess
        from unittest import mock
        from orchestrator import chat as chat_module
        task = self.eng.store.new_task("long job", self.ws.api)
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        keys = list(presses)

        class Reader:
            def read(self, timeout):
                import time
                time.sleep(0.02)
                return keys.pop(0) if keys else ""

            def close(self):
                pass
        deadline = __import__("time").time() + 3

        class Watched:
            pid = proc.pid

            def poll(self):  # a single Esc must not stop it: give up waiting after a moment instead
                return proc.poll() if __import__("time").time() < deadline else 0
        with mock.patch.object(chat_module, "_KeyReader", Reader):
            self.chat._watch(task["id"], Watched())
        return proc

    def test_double_escape_stops_a_running_task(self):
        proc = self._watch_with_keys(["", "\x1b", "\x1b"])
        proc.wait(timeout=5)
        self.assertEqual(proc.returncode, -15, "the task process got SIGTERM")

    def test_single_escape_or_arrow_keys_do_not_stop_it(self):
        proc = self._watch_with_keys(["\x1b", "", "\x1b[A", "\x1b[B"])
        self.assertIsNone(proc.poll())

    def test_accepting_a_git_init_suggestion_makes_foreman_run_it_without_any_ai_or_commit(self):
        import os
        import subprocess
        folder = os.path.join(self.ws.root, "dataset-tool")
        os.makedirs(folder)
        with open(os.path.join(folder, "package.json"), "w") as f:
            f.write('{"name": "dataset-tool"}')
        self.eng.registry.scan()
        task = self.eng.store.new_task("plan the dataset", folder)
        added = self.eng.store.add_suggestions(task["id"], [{
            "project": "dataset-tool", "text": "Run `git init` (no commit) in the project before development",
            "why": "so changes can be detected", "impact": "major"}])
        task["suggestions"] = [added[0]["id"]]
        self.eng.store.save(task)
        self.chat.focus = task["id"]
        out = self.chat.respond(f"do {added[0]['id']}")
        self.assertIn("Ran `git init`", out)
        self.assertTrue(os.path.isdir(os.path.join(folder, ".git")))
        self.assertNotEqual(subprocess.run(["git", "-C", folder, "rev-parse", "HEAD"], capture_output=True).returncode, 0,
                            "no commit was made")
        self.assertEqual(self.s.log, [], "no provider was involved")
        self.assertEqual(self.eng.store.suggestions()[0]["status"], "accepted")
        self.assertIn("already a git repository", self.eng.accept_suggestion(added[0], folder)[1])

    def test_only_a_bare_yes_or_no_gets_the_nothing_is_waiting_answer(self):
        self.assertIn("Nothing is waiting", self.chat.respond("yes"))
        self.assertIn("Nothing is waiting", self.chat.respond("Go ahead!"))
        self.assertNotIn("yes/no", self.chat.respond("go for plan"), "a request, not an answer")
        self.assertNotIn("yes/no", self.chat.respond("ok let's look at the tests"))

    def test_pasted_text_is_sent_in_full_but_shown_and_saved_as_a_chip(self):
        from unittest import mock
        seen = []
        self.s.plan = lambda req: (seen.append(req.prompt) or plan([], [], intent="answer", answer="ok"))
        pasted = "\n".join(f"spec line {i}" for i in range(300))
        echoed = []
        with mock.patch("orchestrator.ui.out", side_effect=echoed.append):
            self.chat._exchange(f"build this: {pasted}", "build this: [Pasted text #1 +300 lines]")
        self.assertIn("spec line 299", seen[0], "the planner receives the whole paste")
        self.assertTrue(any("[Pasted text #1 +300 lines]" in e for e in echoed))
        self.assertFalse(any("spec line 150" in e for e in echoed), "the transcript shows only the chip")
        entry = self.chat.history.data["entries"][-1]
        self.assertEqual(entry["shown"], "build this: [Pasted text #1 +300 lines]")
        self.assertIn("spec line 299", entry["user"])
        self.assertLess(len(self.chat._conversation()), 3000, "later turns carry a shortened copy")

    def test_cross_project_change_waits_for_approval_then_runs_same_plan(self):
        self.s.plan, self.s.step = self._cross_project_plan, editing_worker()
        out = self.chat.respond("show the currency on orders, API and UI")
        self.assertIn("This changes 2 projects", out)
        self.assertIn("orders-api: add currency", out)
        self.assertFalse([l for l in self.s.log if ":step:" in l], "nothing runs before approval")
        out = self.chat.respond("yes")
        self.assertIn("achieved", out)
        self.assertEqual(len(self.prompts), 1, "approval continues the same plan without re-planning")

    def test_approval_feedback_replans_and_no_cancels(self):
        self.s.plan, self.s.step = self._cross_project_plan, editing_worker()
        self.chat.respond("show the currency on orders")
        out = self.chat.respond("keep the UI as is for now")
        self.assertEqual(len(self.prompts), 2)
        self.assertIn("keep the UI as is", self.prompts[1])
        self.assertIn("This changes 2 projects", out, "the revised plan still spans two projects, so ask again")
        out = self.chat.respond("no")
        self.assertIn("Cancelled", out)
        self.assertEqual(self.eng.store.load(self.chat.focus)["status"], "cancelled")
        self.assertFalse([l for l in self.s.log if ":step:" in l])

    def test_single_project_change_runs_without_approval(self):
        self.s.plan = lambda req: plan([("admin-ui", "modify")], [step("1", "admin-ui")])
        self.s.step = editing_worker()
        self.assertIn("achieved", self.chat.respond("add a footer"))

    def test_next_message_answers_a_business_question(self):
        calls = []

        def p(req):
            calls.append(req.prompt)
            q = [] if len(calls) > 1 else [{"text": "Refund shipping too?", "kind": "business"}]
            return plan([("orders-api", "modify")], [step("1", "orders-api")], questions=q)
        self.s.plan, self.s.step = p, editing_worker()
        self.assertIn("Refund shipping too?", self.chat.respond("add refunds"))
        out = self.chat.respond("no, items only")
        worker_prompt = [c.prompt for c in self.eng.providers["claude"].calls if c.mode == "write"][0]
        self.assertIn("A: no, items only", worker_prompt)
        self.assertIn("achieved", out)

    def test_suggestions_accept_and_dismiss_in_plain_language(self):
        sugg = [{"text": "Add a migration for currency", "why": "schema", "project": "orders-api", "impact": "major"},
                {"text": "Update API docs", "why": "contract", "impact": "major"}]
        self.s.plan = lambda req: plan([("admin-ui", "modify")], [step("1", "admin-ui")],
                                       suggestions=sugg if "migration" not in req.prompt.split("REQUEST:")[1][:80] else [])
        self.s.step = editing_worker()
        self.chat.respond("add a footer")
        self.assertIn("Which one?", self.chat.respond("yes"))
        out = self.chat.respond("dismiss the second one")
        self.assertIn("Dismissed s2", out)
        first_task = self.chat.focus
        self.chat.respond("yes, do the first one")
        self.assertNotEqual(self.chat.focus, first_task)
        self.assertEqual(self.eng.store.load(self.chat.focus)["request"], "Add a migration for currency")

    def test_yes_with_nothing_pending_and_conversation_context(self):
        self.assertIn("Nothing is waiting", self.chat.respond("yes"))
        seen = []

        def p(req):
            seen.append(req.prompt)
            return plan([("admin-ui", "inspect")], [], intent="answer", answer="Totals use show().")
        self.s.plan = p
        self.chat.respond("how are totals rendered?")
        self.chat.respond("and where is that tested?")
        self.assertIn("RECENT CONVERSATION", seen[1])
        self.assertIn("how are totals rendered?", seen[1])

    def test_status_and_slash_shortcuts_are_instant(self):
        self.s.plan = lambda req: plan([("admin-ui", "modify")], [step("1", "admin-ui")], title="Footer")
        self.s.step = editing_worker()
        self.chat.respond("add a footer")
        n = len(self.s.log)
        self.assertIn("Footer", self.chat.respond("what's the status of my task?"))
        self.assertIn("Footer", self.chat.respond("/tasks"))
        self.assertIn("/display", self.chat.respond("/help"))
        self.assertEqual(len(self.s.log), n)

    def test_interrupt_marks_task_interrupted_without_notification(self):
        def p(req):
            raise Interrupted()
        self.s.plan = p
        task = self.eng.prepare_new("add a footer", self.ws.ui)
        res = self.eng.execute(task)
        self.assertEqual(res["task"]["status"], "interrupted")
        self.assertEqual(self.notes, [])

    def test_continue_resumes_a_stopped_task(self):
        self.s.plan = lambda req: plan([("admin-ui", "modify")], [step("1", "admin-ui")], title="Footer")
        self.s.step = editing_worker()
        task = self.eng.prepare_new("add a footer", self.ws.ui)
        task["status"] = "interrupted"
        self.eng.store.save(task)
        out = self.chat.respond("continue")
        self.assertIn("achieved", out)
        self.assertEqual(self.chat.focus, task["id"])

    def test_progress_lines(self):
        check = format_event({"event": "check", "project": "api", "command": "npm test", "ok": True})
        self.assertIn("Check", check)
        self.assertIn("(api: npm test)", check)
        self.assertIn("╰ passed", check)
        start = format_event({"event": "step_start", "step_kind": "implement", "project": "api", "provider": "claude"})
        self.assertIn("Implement", start)
        self.assertIn("(api)", start)
        self.assertIsNone(format_event({"event": "created"}))


class ChatStatusLineTest(unittest.TestCase):
    def test_status_line_names_only_the_providers_of_the_last_task(self):
        ws = Workspace()
        try:
            s = Script()
            eng = make_engine(ws, s)
            chat = InProcessChat(ws.ui, engine=eng)
            self.assertIn("planner: claude", chat._hints()[1])
            s.plan = lambda req: plan([("admin-ui", "modify")], [step("1", "admin-ui")])
            s.step = editing_worker()
            chat.respond("add a footer")
            right = chat._hints()[1]
            self.assertIn("last: claude · review: opencode", right)
            out = chat.respond("/tasks")
            self.assertTrue(out)
        finally:
            ws.cleanup()


class ProvidersCommandTest(unittest.TestCase):
    def test_switch_planner_and_first_worker(self):
        ws = Workspace()
        try:
            s = Script()
            eng = make_engine(ws, s)
            chat = InProcessChat(ws.ui, engine=eng)
            self.assertIn("Planner: opencode", chat.respond("/providers opencode"))
            from orchestrator.config import load_config, set_first_provider
            self.assertEqual(load_config(ws.home).brain, "opencode")
            self.assertIn("planner: opencode", chat._hints()[1])
            s.plan = lambda req: plan([("admin-ui", "inspect")], [], intent="answer", answer="ok")
            chat.respond("how are totals shown?")
            self.assertEqual(s.log[-1], "opencode:plan", "the new planner is used immediately")
            self.assertIn("Unknown provider", chat.respond("/providers gemini"))
            set_first_provider(ws.home, "opencode")
            self.assertEqual(list(load_config(ws.home).providers)[0], "opencode")
        finally:
            ws.cleanup()


class SmallTalkTest(unittest.TestCase):
    def test_greetings_are_instant_and_not_tasks(self):
        ws = Workspace()
        try:
            s = Script()
            eng = make_engine(ws, s)
            chat = InProcessChat(ws.ui, engine=eng)
            for text in ("hey", "Hello there!", "thanks", "ok", "good morning"):
                out = chat.respond(text)
                self.assertTrue(out)
            self.assertIn("admin-ui", chat.respond("hi"))
            self.assertEqual(s.log, [], "no model call")
            self.assertEqual(eng.store.all(), [], "nothing stored as a task")
            s.plan = lambda req: plan([("admin-ui", "inspect")], [], intent="answer", answer="x")
            chat.respond("hey, how are totals shown?")
            self.assertEqual(s.log, ["claude:plan"], "real questions still go to the planner")
        finally:
            ws.cleanup()


class ChatHistoryTest(unittest.TestCase):
    def test_conversations_are_saved_per_project_and_resumable(self):
        ws = Workspace()
        try:
            s = Script()
            eng = make_engine(ws, s)
            s.plan = lambda req: plan([("admin-ui", "inspect")], [], intent="answer", answer="Totals use show().")
            chat = InProcessChat(ws.ui, engine=eng)
            chat._exchange("how are totals shown?")
            chat._exchange("hey")
            first_id = chat.history.chat_id
            chat.respond("/new")
            chat._exchange("thanks")
            other = InProcessChat(ws.api, engine=eng)  # a different project has its own history
            self.assertEqual(other.history.list(), [])
            later = InProcessChat(ws.ui, engine=eng)
            chats = later.history.list()
            self.assertEqual([c["title"] for c in chats], ["thanks", "how are totals shown?"])
            later._load_chat(first_id)
            self.assertIn("how are totals shown?", later._conversation(), "context restored for follow-ups")
            seen = []
            s.plan = lambda req: (seen.append(req.prompt), plan([("admin-ui", "inspect")], [], intent="answer",
                                                                 answer="In test.js."))[1]
            later._exchange("and where is that tested?")
            self.assertIn("how are totals shown?", seen[0])
            self.assertEqual(len(later.history.load(first_id)["entries"]), 3, "resumed chat keeps growing")
        finally:
            ws.cleanup()

    def test_fullscreen_restores_terminal_on_exit_and_errors(self):
        import io
        from unittest import mock
        from orchestrator import ui
        buf = io.StringIO()
        with mock.patch.object(ui, "TTY", True), mock.patch("sys.stdout", buf):
            try:
                with ui.FullScreen(True):
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
        self.assertTrue(buf.getvalue().startswith("\x1b[?1049h"))
        self.assertTrue(buf.getvalue().endswith("\x1b[?1049l"))


class PhaseTest(unittest.TestCase):
    """The spinner line follows what is actually happening, not a fixed 'Planning'."""

    def run_events(self, task, events):
        from orchestrator.chat import Phase
        ph = Phase(task)
        seen = [(ph.verb, ph.who)]
        for ev in events:
            ph.update(ev)
            seen.append((ph.verb, ph.who))
        return seen

    def test_question_reads_then_answers(self):
        seen = self.run_events({"request": "which function applies the discount?"}, [
            {"event": "thinking", "provider": "claude", "purpose": "plan"},
            {"event": "activity", "provider": "claude", "where": "plan", "text": "Grep(applyDiscount)"},
            {"event": "activity", "provider": "claude", "where": "plan", "text": "Read(orders-api/src/pricing.js)"},
            {"event": "activity", "provider": "claude", "where": "plan", "text": "· The discount is applied in…"},
            {"event": "plan", "plan": {"steps": []}},
        ])
        verbs = [v for v, _ in seen]
        self.assertEqual(verbs[0], "Looking into it")
        self.assertIn("Searching for applyDiscount", verbs)
        self.assertIn("Reading pricing.js", verbs)
        self.assertIn("Thinking", verbs)
        self.assertEqual(seen[-1], ("Writing the answer", "claude"))
        self.assertNotIn("Planning", " ".join(verbs[1:]))

    def test_change_runs_through_steps_checks_and_review(self):
        seen = self.run_events({"request": "add currency to the orders API"}, [
            {"event": "thinking", "provider": "claude", "purpose": "plan"},
            {"event": "plan", "plan": {"steps": [{"project": "orders-api"}, {"project": "admin-ui"}]}},
            {"event": "step_start", "provider": "claude", "project": "orders-api", "step_kind": "implement"},
            {"event": "step_start", "provider": "antigravity", "project": "admin-ui", "step_kind": "implement"},
            {"event": "activity", "provider": "claude", "where": "orders-api", "text": "Bash(npm test && echo ok)"},
            {"event": "step_done", "project": "orders-api", "status": "done"},
            {"event": "step_done", "project": "admin-ui", "status": "failed"},
            {"event": "check", "project": "orders-api", "command": "npm test", "ok": True},
            {"event": "thinking", "provider": "antigravity", "purpose": "review"},
            {"event": "review", "review": {"verdict": "approve"}},
        ])
        verbs = [v for v, _ in seen]
        self.assertEqual(verbs[0], "Planning the change")
        self.assertIn("Starting work on 2 projects", verbs)
        self.assertIn(("Running npm test in orders-api", "claude"), seen)
        self.assertIn(("Implementing in admin-ui", "antigravity"), seen, "falls back to the step still running")
        self.assertIn("Checking the results", verbs)
        self.assertIn(("Reviewing the changes", "antigravity"), seen)
        self.assertEqual(verbs[-1], "Writing the report")

    def test_resumed_task_says_so(self):
        from orchestrator.chat import intent_verb
        self.assertEqual(intent_verb({"request": "add x", "steps": [{"status": "done"}]}), "Picking up where it left off")


if __name__ == "__main__":
    unittest.main()
