"""How work shows in the chat: tool calls lined up under their step, long output shortened with the rest kept for
the details view (Ctrl+O), failing check output."""

import unittest
from unittest import mock

from helpers import Script, Workspace, make_engine  # noqa: F401  (puts src/ on the path)

from orchestrator import ui
from orchestrator.chat import format_event


class TranscriptTest(unittest.TestCase):
    def test_long_output_is_shortened_and_kept_in_full_for_the_details_view(self):
        text = "\n".join(f"line {i}" for i in range(1, 11))
        short = ui.result(text)
        self.assertIn("line 3", short)
        self.assertNotIn("line 4", short)
        self.assertIn("… +7 lines (ctrl+o to expand)", short)
        self.assertIn("line 10", short.full)
        scroll = ui.Scrollback()
        scroll.add(ui.join("◆ Check(api: npm test)", short))
        self.assertIn("    line 10", scroll.details)
        self.assertNotIn("    line 10", scroll.lines)
        self.assertEqual(ui.result("one line"), "  ╰ one line")

    def test_a_workers_tool_calls_line_up_under_the_first(self):
        first = format_event({"event": "activity", "text": "Read(api/server.js)"})
        then = format_event({"event": "activity", "text": "Bash(npm test)"}, follows=True)
        words = format_event({"event": "activity", "text": "· Checking the totals"}, follows=True)
        self.assertEqual(first, "  ╰ Read(api/server.js)")
        self.assertEqual(then, "    Bash(npm test)")
        self.assertEqual(words, "    Checking the totals")

    def test_a_failing_check_shows_the_start_of_its_output(self):
        tail = "\n".join(f"FAIL test_{i}" for i in range(12))
        shown = format_event({"event": "check", "project": "api", "command": "npm test", "ok": False, "tail": tail})
        self.assertIn("failed", shown)
        self.assertIn("FAIL test_0", shown)
        self.assertIn("+9 lines (ctrl+o to expand)", shown)
        self.assertIn("FAIL test_11", shown.full)

    def test_engine_records_the_failing_output_of_a_check(self):
        ws = Workspace()
        try:
            eng = make_engine(ws, Script())
            eng.registry.ensure_scanned()
            task = eng.store.new_task("x", ws.api)
            task["checks"] = [{"project": "orders-api", "command": "npm test"}]
            with mock.patch("orchestrator.engine.run_process",
                            return_value=mock.Mock(code=1, out="boom\nmore", err="", timed_out=False)):
                eng._run_checks(task, {})
            ev = [e for e in eng.store.events(task["id"]) if e["event"] == "check"][0]
            self.assertEqual(ev["tail"], "boom\nmore")
        finally:
            ws.cleanup()


class ScrolledViewTest(unittest.TestCase):
    """Scrolled up in the full-screen chat: the last message you sent above the view stays pinned at the top, and a
    bar at the bottom jumps back and counts what arrived meanwhile."""

    class Live:
        notice = ""

        def live_height(self):
            return 3

        def frame(self):
            return ""

    def scroll(self):
        s = ui.Scrollback()
        s.enabled = True
        s.push(self.Live())
        s.add("> how do the totals work?")
        for i in range(40):
            s.add(f"answer line {i}")
        return s

    def test_prompt_pinned_and_jump_bar(self):
        s = self.scroll()
        with mock.patch("shutil.get_terminal_size", return_value=__import__("os").terminal_size((80, 13))):
            s.scroll(10)
            shown = s._visible()
            self.assertIn("↑ > how do the totals work?", shown[0])
            s.add("◆ new result")
            s.add("more")
            self.assertIn("2 new messages", s.jump_bar())
            s.mouse(f"\x1b[<0;5;{len(s._shown) + 1}M")  # a click on the bar
            self.assertEqual((s.offset, s.unseen), (0, 0))
            s.scroll(10)
            s._visible()
            s.mouse("\x1b[<0;5;1M")  # a click on the pinned message scrolls to it
            self.assertIn("> how do the totals work?", s._visible()[0])

    def test_nothing_pinned_while_following_the_output(self):
        s = self.scroll()
        with mock.patch("shutil.get_terminal_size", return_value=__import__("os").terminal_size((80, 13))):
            self.assertNotIn("↑", s._visible()[0])


class RenameTest(unittest.TestCase):
    def test_rename_names_the_conversation_and_the_terminal_title(self):
        from orchestrator.chat import Chat
        ws = Workspace()
        try:
            chat = Chat(ws.api, engine=make_engine(ws, Script()))
            titles = []
            with mock.patch("orchestrator.ui.set_title", side_effect=titles.append):
                out = chat.respond("/rename Dataset merge")
            self.assertIn("Renamed", out)
            self.assertEqual(chat.history.data["title"], "Dataset merge")
            self.assertEqual(titles[-1], "Foreman · Dataset merge")
        finally:
            ws.cleanup()


class StatusLineTest(unittest.TestCase):
    def test_status_line_under_the_input_and_off_switch(self):
        from orchestrator.chat import Chat
        ws = Workspace()
        try:
            chat = Chat(ws.api, engine=make_engine(ws, Script()))
            line = chat._status_line()
            self.assertTrue(line.startswith("orders-api · auto mode"), line)
            e = ui.LineEditor([])
            e.buf, e.cur, e.sel, e.row, e.hints, e.status = "", 0, 0, 0, ("", ""), line
            self.assertIn("◈ orders-api", e._below()[-1])
            chat.eng.cfg.status_line = False
            self.assertEqual(chat._status_line(), "")
            self.assertIn("orders-api", chat._hints()[1], "without it, the hint shows the project again")
        finally:
            ws.cleanup()


class PrettyTextTest(unittest.TestCase):
    def test_plain_command_output_gets_structure_and_colour(self):
        with mock.patch.object(ui, "TTY", True):
            text = ui.pretty("Lifecycle of api:\n  orders     Development (done, 2026-10-26) → next: QA\n"
                             "Recommended: QA for orders. Say \"next\" to start it.")
            lines = text.split("\n")
            self.assertIn(ui.BOLD + ui.ACCENT + "Lifecycle of api", lines[0], "a 'Heading:' line")
            self.assertIn(ui.BOLD + "orders" + ui.RESET, lines[1], "the key of a two-column row")
            self.assertIn(ui.GREEN + "done", lines[1], "states in colour")
            self.assertIn(ui.BOLD + "Recommended:", lines[2])
            self.assertIn(ui.BLUE + '"next"', lines[2], "a quoted command")
            self.assertIn(ui.ACCENT + "#12", ui.emphasize("see #12"))
            self.assertNotIn(ui.BLUE, ui.emphasize('{"id": "x1", "source": "off"}'), "JSON stays plain")

    def test_lifecycle_and_tasks_views(self):
        from orchestrator.chat import Chat
        ws = Workspace()
        try:
            chat = Chat(ws.api, engine=make_engine(ws, Script()))
            data = {"project": "orders-api", "modes": [], "decisions": 2, "recommended": {"feature": "vip", "mode": "qa"},
                    "features": {"vip": {"stage": "development", "status": "done", "updated": "2026-10-26T10:00",
                                         "next": "qa", "next_reason": "test it"}}}
            with mock.patch.object(chat.eng, "lifecycle_data", return_value=data):
                view = chat._lifecycle_view()
            for part in ("Lifecycle of orders-api", "vip", "Development", "done", "2026-10-26", "next: ", "QA",
                         "Recommended: ", "2 confirmed decisions"):
                self.assertIn(part, view)
            chat.eng.store.new_task("add currency", ws.api)
            self.assertIn("add currency", chat._tasks_view())
        finally:
            ws.cleanup()


if __name__ == "__main__":
    unittest.main()
