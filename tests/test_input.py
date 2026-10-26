"""The chat input: line-wise editing keys, paste-back, several lines, the grey suggestion, '@' files, dropped files."""

import os
import tempfile
import unittest

from helpers import Script, Workspace, make_engine  # noqa: F401  (puts src/ on the path)

from orchestrator import ui
from orchestrator.clipboard import Attachments, dropped_files, image_dirs


def editor(root=None):
    e = ui.LineEditor([("/help", "help")], root=root)
    e.buf, e.cur, e.sel, e.row, e.hist_i, e.hints = "", 0, 0, 0, 0, ("", "")
    return e


def typed(e, text):
    for ch in text:
        e._key(ch)


class EditingKeysTest(unittest.TestCase):
    def test_ctrl_u_deletes_only_the_current_line_and_ctrl_y_pastes_it_back(self):
        e = editor()
        typed(e, "first line\\\rsecond line")
        self.assertEqual(e.buf, "first line\nsecond line")
        e._key("\x15")
        self.assertEqual(e.buf, "first line\n", "only the line the cursor is on")
        e._key("\x15")
        self.assertEqual(e.buf, "first line", "on an empty line it joins the line above")
        e._key("\x19")
        self.assertEqual(e.buf, "first line\n", "Ctrl+Y gives back the last deletion (the line break)")
        e = editor()
        typed(e, "hello world")
        e._key("\x17")
        self.assertEqual(e.buf, "hello ")
        e._key("\x19")
        self.assertEqual(e.buf, "hello world")

    def test_ctrl_a_and_ctrl_e_stay_on_the_current_line(self):
        e = editor()
        typed(e, "one\\\rtwo")
        e._key("\x01")
        self.assertEqual(e.cur, 4)
        e._key("\x05")
        self.assertEqual(e.cur, 7)
        e._key("\x01")
        e._key("\x0b")
        self.assertEqual(e.buf, "one\n")
        e._key("\x19")
        self.assertEqual(e.buf, "one\ntwo")

    def test_backslash_enter_adds_a_line_and_plain_enter_sends(self):
        e = editor()
        typed(e, "a\\")
        self.assertEqual(e._key("\r"), ("", None))
        typed(e, "b")
        self.assertEqual(e._key("\r"), ("", "a\nb"))

    def test_up_and_down_move_between_lines_before_history(self):
        e = editor()
        e.history = ["old message"]
        e.hist_i = 1
        typed(e, "abcdef\\\rxy")
        e._key("\x1b[A")
        self.assertEqual((e.buf, e.cur), ("abcdef\nxy", 2), "same column on the line above")
        e._key("\x1b[A")
        self.assertEqual(e.buf, "old message", "on the first line, ↑ is history")

    def test_each_line_gets_its_own_rows(self):
        e = editor()
        typed(e, "one\\\rtwo")
        frame = e._frame()
        self.assertIn("one", frame)
        self.assertIn("\r\n  two", frame, "the second line starts under the prompt, indented")
        self.assertEqual(e.live_height(), 2 + 2 + 1)


class SuggestionTest(unittest.TestCase):
    def test_grey_suggestion_is_taken_with_tab_and_dropped_when_typing(self):
        e = editor()
        e.suggestion = "Run QA on the dataset"
        self.assertIn("Run QA on the dataset", e._frame())
        self.assertIn("tab", e._below()[0])
        e._key("\t")
        self.assertEqual(e.buf, "Run QA on the dataset")
        e = editor()
        e.suggestion = "Run QA"
        e._key("x")
        self.assertEqual((e.buf, e.suggestion), ("x", ""))
        e._key("\x7f")
        self.assertNotIn("Run QA", e._frame(), "gone for good once you typed")


class MentionTest(unittest.TestCase):
    def test_at_lists_project_files_and_enter_completes_without_sending(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "src"))
            for name in ("src/server.py", "src/models.py", "README.md"):
                open(os.path.join(root, name), "w").close()
            e = editor(root)
            typed(e, "look at @serv")
            self.assertEqual([m[0] for m in e._menu()], ["@src/server.py"])
            self.assertEqual(e._key("\r"), ("", None))
            self.assertEqual(e.buf, "look at @src/server.py ")
            typed(e, "and @sr")
            self.assertIn("@src/", [m[0] for m in e._menu()])
            self.assertEqual(e._menu(), e.file_matches())
            e = editor(root)
            typed(e, "mail me@x")
            self.assertEqual(e.file_matches(), [], "an @ inside a word is not a mention")


class DroppedFilesTest(unittest.TestCase):
    def test_any_dropped_file_becomes_a_file_chip_with_its_path_for_the_ai(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "data set.csv")
            open(path, "w").close()
            self.assertEqual(dropped_files(f"'{path}'"), [path])
            e = editor()
            e._paste(f"'{path}'")
            self.assertEqual(e.buf, "[File #1 data set.csv] ")
            self.assertEqual(e.attach.expand(e.buf.strip()), f"[File #1: {path}]")
            self.assertEqual(image_dirs(f"see [File #1: {path}]"), [tmp], "the worker may read its folder")
            self.assertEqual(image_dirs(f"see [File #1: {tmp}]"), [tmp], "an attached folder itself")
            e._key("\x7f")
            self.assertEqual(e.buf, "[File #1 data set.csv]")

    def test_attachments_number_files_separately(self):
        a = Attachments()
        self.assertEqual(a.file("/x/a.csv"), "[File #1 a.csv]")
        self.assertEqual(a.image("/x/b.png"), "[Image #1]")
        self.assertEqual(a.file("/x/c"), "[File #2 c]")


class SpinnerInputTest(unittest.TestCase):
    def test_queued_message_comes_back_with_up_and_backslash_enter_adds_a_line(self):
        s = ui.Spinner(footer=False)
        s._type("fix it\r")
        self.assertEqual([str(q) for q in s.queued], ["fix it"])
        s._type("\x1b[A")
        self.assertEqual((s.typed, s.queued), ("fix it", []))
        s._type(" now\\\rsecond")
        self.assertEqual(s.typed, "fix it now\nsecond")
        s._type("\x15")
        self.assertEqual(s.typed, "fix it now\n")


if __name__ == "__main__":
    unittest.main()
