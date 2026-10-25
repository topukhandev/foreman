"""Pasted text and images: chips, the 2000-line limit, dropped image files, the editor's handling of chips, and prompts
too long for a command line."""

import os
import pty
import select
import struct
import sys
import tempfile
import termios
import time
import unittest
import fcntl

from helpers import Script, Workspace, make_engine  # noqa: F401  (puts src/ on the path)

from orchestrator import ui
from orchestrator.clipboard import MAX_PASTE_LINES, Attachments, dropped_images, image_dirs
from orchestrator.providers.base import MAX_PROMPT_BYTES, RunRequest, fit_prompt

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def lines(n):
    return "\n".join(f"line {i}" for i in range(1, n + 1))


class AttachmentsTest(unittest.TestCase):
    def test_short_paste_stays_inline(self):
        a = Attachments()
        self.assertEqual(a.paste("fix the total", "")[0], "fix the total")
        self.assertEqual(a.paste("one\ntwo\nthree", "")[0], "one\ntwo\nthree")

    def test_long_paste_becomes_a_numbered_chip_and_expands_back(self):
        a = Attachments()
        text = lines(545)
        chip, note = a.paste(text, "")
        self.assertEqual(chip, "[Pasted text #1 +545 lines]")
        self.assertEqual(note, "")
        second, _ = a.paste(lines(10), chip)
        self.assertEqual(second, "[Pasted text #2 +10 lines]")
        self.assertEqual(a.expand(f"look: {chip} and {second}"), f"look: {text} and {lines(10)}")

    def test_long_single_line_shows_characters(self):
        chip, _ = Attachments().paste("x" * 1234, "")
        self.assertEqual(chip, "[Pasted text #1 1,234 chars]")

    def test_trailing_newline_of_a_paste_is_dropped(self):
        a = Attachments()
        chip, _ = a.paste(lines(6) + "\n", "")
        self.assertEqual(a.expand(chip), lines(6))

    def test_paste_over_the_limit_is_cut(self):
        a = Attachments()
        chip, note = a.paste(lines(3120), "")
        self.assertIn("+2000 lines", chip)
        self.assertIn("Kept 2000 of 3120", note)
        self.assertEqual(a.expand(chip).count("\n") + 1, MAX_PASTE_LINES)

    def test_limit_covers_all_pastes_in_one_message(self):
        a = Attachments()
        first, _ = a.paste(lines(1500), "")
        second, note = a.paste(lines(1500), first)
        self.assertIn("+500 lines", second)
        self.assertIn("limit", note)
        third, note = a.paste(lines(50), first + second)
        self.assertEqual(third, "")
        self.assertIn("Paste limit reached", note)
        # once a chip is deleted from the message its lines are free again
        again, _ = a.paste(lines(50), second)
        self.assertIn("+50 lines", again)

    def test_unknown_chip_text_is_left_alone(self):
        self.assertEqual(Attachments().expand("see [Pasted text #9 +5 lines]"), "see [Pasted text #9 +5 lines]")

    def test_image_chip_expands_to_its_path(self):
        a = Attachments()
        chip = a.image("/tmp/shot.png")
        self.assertEqual(chip, "[Image #1]")
        self.assertEqual(a.expand(f"what is wrong here? {chip}"), "what is wrong here? [Image #1: /tmp/shot.png]")


class DroppedImagesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.img = os.path.join(self.tmp, "my shot.png")
        self.img2 = os.path.join(self.tmp, "b.JPG")
        for p in (self.img, self.img2):
            with open(p, "wb") as f:
                f.write(PNG)
        self.txt = os.path.join(self.tmp, "notes.txt")
        open(self.txt, "w").close()

    def test_every_way_a_terminal_pastes_a_dragged_file(self):
        for pasted in (f"'{self.img}'", self.img.replace(" ", "\\ "), "file://" + self.img.replace(" ", "%20"),
                       f'"{self.img}"\n'):
            self.assertEqual(dropped_images(pasted), [self.img], pasted)

    def test_several_files(self):
        self.assertEqual(dropped_images(f"'{self.img}' {self.img2}"), [self.img, self.img2])

    def test_anything_else_is_ordinary_text(self):
        for pasted in ("", "hello", self.txt, f"{self.img} and more words", os.path.join(self.tmp, "missing.png"), "'unbalanced"):
            self.assertEqual(dropped_images(pasted), [], pasted)

    def test_image_dirs_from_a_request(self):
        request = f"look at [Image #1: {self.img}] and [Image #2: {self.img2}] and [Image #3: /nope/x.png]"
        self.assertEqual(image_dirs(request), [self.tmp])


class EditorChipsTest(unittest.TestCase):
    def editor(self):
        e = ui.LineEditor([])
        e.buf, e.cur, e.sel, e.row, e.hist_i, e.hints = "", 0, 0, 0, 0, ("", "")
        return e

    def test_paste_inserts_a_chip_at_the_cursor_and_backspace_removes_it_whole(self):
        e = self.editor()
        e._insert("check ")
        e._paste(lines(30))
        self.assertEqual(e.buf, "check [Pasted text #1 +30 lines]")
        e._key("\x7f")
        self.assertEqual(e.buf, "check ")
        self.assertEqual(e.cur, 6)

    def test_arrow_keys_and_delete_treat_a_chip_as_one_character(self):
        e = self.editor()
        e._paste(lines(30))
        e._insert(" ok")
        e.cur = 0
        e._key("\x1b[C")
        self.assertEqual(e.cur, len("[Pasted text #1 +30 lines]"))
        e._key("\x1b[D")
        self.assertEqual(e.cur, 0)
        e._key("\x1b[3~")
        self.assertEqual(e.buf, " ok")

    def test_bracketed_paste_sequence_reaches_the_buffer(self):
        e = self.editor()
        rest, result = e._key("\x1b[200~" + lines(8) + "\x1b[201~tail")
        self.assertEqual((rest, result), ("tail", None))
        self.assertTrue(e.buf.startswith("[Pasted text #1 +8 lines]"))

    def test_incomplete_paste_waits_for_the_rest(self):
        e = self.editor()
        self.assertEqual(e._key("\x1b[200~" + lines(8)), (None, None))

    def test_dropped_image_becomes_a_chip(self):
        e = self.editor()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.png")
            with open(path, "wb") as f:
                f.write(PNG)
            e._paste(f"'{path}'")
        self.assertEqual(e.buf, "[Image #1] ")

    def test_overlong_paste_sets_a_notice_that_clears_on_the_next_key(self):
        e = self.editor()
        e._paste(lines(2500))
        self.assertIn("limit", e.notice)
        self.assertIn(e.notice[:20], e._below()[0])
        e._key("a")
        self.assertEqual(e.notice, "")


class PromptSizeTest(unittest.TestCase):
    def test_small_prompt_is_untouched(self):
        req = RunRequest(prompt="hi", cwd=".")
        self.assertIs(fit_prompt(req), req)

    def test_huge_prompt_moves_to_a_file_the_cli_is_pointed_at(self):
        big = "start " + "word " * (MAX_PROMPT_BYTES // 4) + " the end"
        req = fit_prompt(RunRequest(prompt=big, cwd="."))
        self.assertLess(len(req.prompt.encode()), MAX_PROMPT_BYTES // 4)
        self.assertIn("start ", req.prompt)
        self.assertIn("the end", req.prompt)
        folder = req.extra_dirs[-1]
        path = next(w for w in req.prompt.splitlines() if w.startswith(folder))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), big)
        os.remove(path)


class PtyPasteTest(unittest.TestCase):
    """The real editor in a pseudo-terminal: a 545-line paste shows as one chip and is submitted in full."""

    SCRIPT = ("import sys; sys.path.insert(0, 'src')\n"
              "from orchestrator import ui\n"
              "r = ui.read_input()\n"
              "print('RESULT', len(r), len(r.shown), r.shown.replace(chr(10), '|'))\n")

    def test_long_paste_in_a_terminal(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(root)
            os.environ["TERM"] = "xterm"
            os.execvp(sys.executable, [sys.executable, "-c", self.SCRIPT])
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 12, 70, 0, 0))
        out = b""

        def drain(seconds):
            nonlocal out
            end = time.time() + seconds
            while time.time() < end and select.select([fd], [], [], 0.2)[0]:
                try:
                    out += os.read(fd, 65536)
                except OSError:
                    return
        drain(1.0)
        text = lines(545).replace("\n", "\r")  # terminals send Enter as CR inside a paste
        os.write(fd, b"\x1b[200~" + text.encode() + b"\x1b[201~")
        drain(0.8)
        screen = out.decode(errors="replace")
        self.assertIn("[Pasted text #1 +545 lines]", screen)
        self.assertNotIn("line 300", screen, "the pasted text itself is never drawn")
        os.write(fd, b"\r")
        drain(1.0)
        result = out.decode(errors="replace").split("RESULT ")[-1].split("\r\n")[0]
        size, shown_size, shown = result.split(" ", 2)
        self.assertEqual(int(size), len(lines(545)))
        self.assertEqual(shown, "[Pasted text #1 +545 lines]")
        os.waitpid(pid, 0)


if __name__ == "__main__":
    unittest.main()
