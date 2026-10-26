"""Chat UI pieces that don't need a real terminal: line-editor keys, slash menu, wrapping, provider health."""

import unittest

from helpers import Script, Workspace, make_engine

from orchestrator import ui
from orchestrator.chat import COMMANDS, format_event
from orchestrator.providers.base import RunResult
from test_scenarios import editing_worker, plan, step


def feed(ed: ui.LineEditor, keys: str):
    ed.buf, ed.cur, ed.sel, ed.row, ed.hist_i = "", 0, 0, 0, len(ed.history)
    rest, result = keys, None
    while rest:
        rest, result = ed._key(rest)
        if result is not None:
            return result
    return ed.buf


class LineEditorTest(unittest.TestCase):
    def setUp(self):
        self.ed = ui.LineEditor(COMMANDS)

    def test_slash_menu_filters_and_completes(self):
        self.assertEqual([c for c, _ in self.ed.matches("/st")], ["/status"])
        self.assertEqual(len(self.ed.matches("/")), len(COMMANDS))
        self.assertEqual(self.ed.matches("/show 3"), [], "menu closes once arguments start")
        self.assertEqual(feed(self.ed, "/ta\r"), "/tasks", "Enter picks the highlighted command")
        self.assertEqual(feed(self.ed, "/\x1b[B\x1b[B\t"), "/tasks ", "arrows move, Tab completes")

    def test_editing_keys_and_history(self):
        self.assertEqual(feed(self.ed, "helo\x1b[Dl\r"), "hello")
        self.assertEqual(feed(self.ed, "one two\x17three\r"), "one three")
        self.assertIs(feed(self.ed, "\x03"), KeyboardInterrupt)
        self.assertEqual(feed(self.ed, "abc\x03x\r"), "x", "Ctrl+C clears a non-empty line")
        self.ed.history = ["first", "second"]
        self.assertEqual(feed(self.ed, "\x1b[A\x1b[A\r"), "first")

    def test_bracketed_paste_keeps_newlines(self):
        self.assertEqual(feed(self.ed, "\x1b[200~line1\r\nline2\x1b[201~\r"), "line1\nline2")

    def test_wrap_ignores_colour_codes(self):
        text = " ".join([ui.c("word", ui.BLUE)] * 60)
        for line in ui.wrap_block(text).splitlines():
            self.assertLessEqual(ui.vlen(line), ui.width())
        self.assertGreater(ui.vlen(ui.wrap_block(text).splitlines()[0]), ui.width() - 10)


class ProviderHealthTest(unittest.TestCase):
    def test_expired_login_is_skipped_and_explained(self):
        ws = Workspace()
        try:
            s = Script()
            eng = make_engine(ws, s)
            s.plan = lambda req: plan([("admin-ui", "modify")], [step("1", "admin-ui")])
            s.step = editing_worker()
            orig = eng.providers["opencode"].handler
            eng.providers["opencode"].handler = lambda req: RunResult(False, error="This API key has expired.") \
                if req.schema and "verdict" in req.schema["properties"] else orig(req)
            eng.handle("add a footer", ws.ui)
            self.assertIn("opencode", eng.unhealthy())
            events = [e["event"] for e in eng.store.events("1")]
            self.assertIn("thinking_failed", events)
            calls = len(eng.providers["opencode"].calls)
            eng.handle("add a header", ws.ui)
            self.assertEqual(len(eng.providers["opencode"].calls), calls, "skipped while its login is broken")
        finally:
            ws.cleanup()

    def test_activity_paths_use_project_names(self):
        ws = Workspace()
        try:
            eng = make_engine(ws, Script())
            eng.registry.ensure_scanned()
            eng._activity("9", "claude", "plan", f"Read({ws.api}/server.js)")
            eng._activity("9", "claude", "plan", f"Read({ws.api}/server.js)")  # duplicate dropped
            eng._activity("9", "claude", "review", f"Read({ws.api}/{'deep/' * 30}x.js)")  # long path: name, then cut
            acts = [e for e in eng.store.events("9") if e["event"] == "activity"]
            self.assertEqual(acts[0]["text"], "Read(orders-api/server.js)")
            self.assertTrue(acts[1]["text"].startswith("Read(orders-api/deep/") and acts[1]["text"].endswith("…)"))
            acts = acts[:1]
            line = format_event(acts[0], current="other")
            self.assertIn("[plan] Read(orders-api/server.js)", line)
        finally:
            ws.cleanup()



class CleanTextTest(unittest.TestCase):
    def test_double_escaped_answers_are_repaired(self):
        from orchestrator.engine import clean_text
        self.assertEqual(clean_text('Run it.\\n\\nThen check."'), "Run it.\n\nThen check.")
        self.assertEqual(clean_text('Say "hi" twice'), 'Say "hi" twice')
        self.assertEqual(clean_text("line1\nline2 has a \\n literal"), "line1\nline2 has a \\n literal")


class SelectionTest(unittest.TestCase):
    """Mouse selection in the chat: press, drag, release copies the highlighted text and says how much."""

    class Live:
        notice = ""

        def live_height(self):
            return 3

        def frame(self):
            return ""

    def setUp(self):
        from unittest import mock
        self.scroll = ui.Scrollback()
        self.scroll.enabled = self.scroll.select_enabled = True
        self.live = self.Live()
        self.scroll.push(self.live)
        self.copied = []
        self.patches = [mock.patch("orchestrator.ui.copy_text", side_effect=lambda t: self.copied.append(t) or True),
                        mock.patch("orchestrator.ui.SCROLL", self.scroll),
                        mock.patch("orchestrator.ui.shutil.get_terminal_size", return_value=__import__("os").terminal_size((40, 12)))]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def event(self, button, x, y, kind="M"):
        return self.scroll.mouse(f"\x1b[<{button};{x};{y}{kind}")

    def drag(self, start, end):
        self.event(0, *start)
        self.event(32, *end)
        self.event(0, *end, kind="m")

    def test_dragging_over_two_rows_copies_the_text_and_reports_the_size(self):
        self.scroll.add("first line here\nsecond line there")
        self.drag((7, 1), (6, 2))  # from "line here" to "second"
        self.assertEqual(self.copied, ["line here\nsecond"])
        self.assertEqual(self.live.notice, "16 characters copied to clipboard")

    def test_the_highlight_is_reverse_video_on_the_selected_columns(self):
        self.assertEqual(ui.highlight("abcdef", 1, 2), "a\033[7mbc\033[27mdef")
        bold = ui.highlight("\033[1mabcdef\033[0m", 4, 99)
        self.assertEqual(bold, "\033[1mabcd\033[7mef\033[0m\033[7m\033[27m")  # a reset inside the selection keeps it
        self.assertEqual(ui.cells("日a")[1], ("a", 2, 1))

    def test_a_wrapped_line_is_copied_as_one_line(self):
        self.scroll.add("x" * 60)  # wraps at 40 columns
        self.drag((1, 1), (20, 2))
        self.assertEqual(self.copied, ["x" * 60])

    def test_a_plain_click_copies_nothing_and_the_wheel_still_scrolls(self):
        self.scroll.add("some text")
        self.event(0, 3, 1)
        self.event(0, 3, 1, kind="m")
        self.assertEqual(self.copied, [])
        self.assertFalse(self.event(64, 3, 1), "wheel events are not selection events")

    def test_clicks_below_the_transcript_do_not_start_a_selection(self):
        self.scroll.add("some text")
        self.event(0, 3, 9)
        self.event(32, 5, 1)
        self.event(0, 5, 1, kind="m")
        self.assertEqual(self.copied, [])

    def test_selection_is_off_when_disabled(self):
        self.scroll.select_enabled = False
        self.scroll.add("some text")
        self.assertFalse(self.event(0, 1, 1))

    def test_a_key_or_new_output_clears_the_highlight(self):
        self.scroll.add("some text here")
        self.drag((1, 1), (4, 1))
        self.assertTrue(self.scroll.sel)
        self.scroll.bottom()
        self.assertIsNone(self.scroll.sel)


class DoubleEscTest(unittest.TestCase):
    def test_two_lone_escapes_within_the_window(self):
        esc = ui.DoubleEsc(window=1.0)
        self.assertEqual(esc.feed("\x1b", now=10.0), "first")
        self.assertEqual(esc.feed("\x1b", now=10.5), "second")
        self.assertEqual(esc.feed("\x1b", now=10.6), "first", "a third press starts over")

    def test_too_slow_or_other_keys_do_not_count(self):
        esc = ui.DoubleEsc(window=1.0)
        esc.feed("\x1b", now=10.0)
        self.assertEqual(esc.feed("\x1b", now=12.0), "first")
        self.assertEqual(esc.feed("\x1b[A", now=12.1), "")
        self.assertEqual(esc.feed("a", now=12.2), "")


class ClipTest(unittest.TestCase):
    def test_long_line_becomes_one_row_with_ellipsis(self):
        line = ui.c("› " + "word " * 40, ui.BOLD)
        clipped = ui.clip(line, 30)
        self.assertEqual(len(ui.hard_wrap(clipped, 30)), 1)
        self.assertIn("…", clipped)

    def test_short_line_is_untouched(self):
        self.assertEqual(ui.clip("short", 30), "short")

    def test_wide_characters_are_measured_by_columns(self):
        clipped = ui.clip("日本語" * 20, 20)
        self.assertEqual(len(ui.hard_wrap(clipped, 20)), 1)


class MarkdownTest(unittest.TestCase):
    def test_links_render_as_names(self):
        out = ui.ANSI.sub("", ui.md("See [`listOrders()`](file:///a/b/server.js#L25-L34) and [docs](https://x.y)."))
        self.assertEqual(out, "See listOrders() and docs.")


class ScrollbackTest(unittest.TestCase):
    """The full-screen chat scrolls its own transcript: wheel/PgUp/PgDn move the view, keys don't leak into input."""

    class Live:
        def __init__(self, h):
            self.h, self.redraws = h, 0

        def live_height(self):
            return self.h

        def frame(self):
            self.redraws += 1
            return ""

    def setUp(self):
        import io
        import shutil
        from unittest import mock
        self.sb = ui.Scrollback()
        self.sb.enabled = True
        self.screen = io.StringIO()
        patches = [mock.patch.object(ui, "TTY", True), mock.patch.object(ui, "SCROLL", self.sb),
                   mock.patch("sys.stdout", self.screen),
                   mock.patch.object(shutil, "get_terminal_size", return_value=shutil.os.terminal_size((40, 10)))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def shown(self):
        """Rows painted by the last render (between the home sequence and the clear-below)."""
        last = self.screen.getvalue().rsplit("\x1b[H", 1)[-1].split(ui.SYNC_OFF)[0]
        return [ui.ANSI.sub("", r) for r in last.split("\r\n") if ui.ANSI.sub("", r)]

    def test_keys_are_split_and_classified(self):
        keys, rest = ui.split_keys("a\x1b[<64;5;7M\x1b[5~\x1b[A\x1b[<65;1;1")
        self.assertEqual(keys, ["a", "\x1b[<64;5;7M", "\x1b[5~", "\x1b[A"])
        self.assertEqual(rest, "\x1b[<65;1;1", "a split mouse event waits for the rest")
        self.assertEqual(ui.scroll_amount("\x1b[<64;5;7M"), 3)
        self.assertEqual(ui.scroll_amount("\x1b[<65;5;7M"), -3)
        self.assertEqual(ui.scroll_amount("\x1b[<0;5;7M"), 0, "clicks are swallowed")
        self.assertEqual(ui.scroll_amount("\x1b[6~"), -ui.PAGE)
        self.assertIsNone(ui.scroll_amount("\x1b[A"), "arrows stay for message history")

    def test_long_lines_wrap_to_rows_keeping_colour(self):
        rows = ui.hard_wrap(ui.c("x" * 50, ui.GREEN), 40)
        self.assertEqual([ui.vlen(r) for r in rows], [40, 10])
        self.assertTrue(rows[1].startswith(ui.GREEN))

    def test_scroll_to_the_very_top_and_back(self):
        live = self.Live(3)  # 10 rows - 3 live = 7 rows of transcript
        self.sb.push(live)
        self.sb.add("\n".join(f"line {i}" for i in range(1, 21)))
        self.sb.scroll(3)
        self.sb.flush()
        self.assertEqual(self.sb.offset, 3)
        self.assertEqual(self.shown()[-2], "line 17")
        self.assertIn("Jump to bottom", self.shown()[-1])
        self.assertIn("3 lines below", self.shown()[-1])
        for _ in range(10):
            self.sb.scroll(ui.PAGE)
        self.sb.flush()
        self.assertEqual(self.shown()[0], "line 1", "top content shows from its first line")
        self.sb.add("new output")  # arriving while scrolled: the view stays put
        self.assertEqual(self.shown()[0], "line 1")
        self.sb.bottom()
        self.sb.flush()
        self.assertEqual(self.sb.offset, 0)
        self.assertEqual(self.shown()[-1], "new output")
        self.assertEqual(len(self.shown()), 7)
        self.assertGreaterEqual(live.redraws, 3, "the input box is redrawn under the transcript")

    def test_short_transcript_does_not_scroll(self):
        self.sb.push(self.Live(3))
        self.sb.add("only\nthree\nlines")
        self.sb.scroll(3)
        self.assertEqual(self.sb.offset, 0)

    def test_editor_scrolls_instead_of_typing_or_history(self):
        ed = ui.LineEditor(COMMANDS)
        ed.history = ["earlier message"]
        self.sb.push(self.Live(3))
        self.sb.add("\n".join(str(i) for i in range(30)))
        self.assertEqual(feed(ed, "\x1b[<64;3;3M\x1b[<64;3;3M\x1b[5~"), "")
        self.assertGreater(self.sb.offset, 6)
        self.assertEqual(feed(ed, "\x1b"), "", "Esc while scrolled jumps back without clearing")
        self.assertEqual(self.sb.offset, 0)
        self.sb.scroll(3)
        self.assertEqual(feed(ed, "hi"), "hi", "typing returns to the bottom")
        self.assertEqual(self.sb.offset, 0)
        self.assertEqual(feed(ed, "\x1b[A"), "earlier message")
        self.assertEqual(ed._key("\x1b[<64;3"), (None, None), "incomplete mouse event is not typed")


    def test_a_burst_of_wheel_events_is_one_flicker_free_frame(self):
        ed = ui.LineEditor(COMMANDS)
        ed.buf, ed.cur, ed.row, ed.sel, ed.hints = "draft", 5, 0, 0, ("", "")
        self.sb.push(ed)
        self.sb.add("\n".join(str(i) for i in range(60)))
        rest = "\x1b[<64;3;3M" * 5
        while rest:
            rest, _ = ed._key(rest)
        self.assertTrue(self.sb.flush())
        out = self.screen.getvalue()
        self.assertEqual(out.count("\x1b[H"), 1, "five wheel steps, one repaint")
        self.assertTrue(out.startswith(ui.SYNC_ON), "synchronized, cursor hidden while drawing")
        self.assertNotIn("\x1b[2K", out, "lines are overwritten, never blanked first")
        self.assertIn("> draft", ui.ANSI.sub("", out), "the input box keeps what was typed")
        self.assertEqual(self.sb.offset, 15)


if __name__ == "__main__":
    unittest.main()
