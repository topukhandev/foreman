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


if __name__ == "__main__":
    unittest.main()
