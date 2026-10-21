"""Tests for interactive terminal input reader and slash command dropdown navigation."""

import os
import pty
import select
import sys
import unittest

from orchestrator.input_reader import (
    COMMANDS,
    get_command_matches,
    render_dropdown_box,
    read_interactive_line
)


class TestInputReader(unittest.TestCase):
    """Test slash command matching and interactive dropdown."""

    def test_get_command_matches(self):
        """Test command filtering by prefix."""
        all_matches = get_command_matches("/")
        self.assertEqual(len(all_matches), len(COMMANDS))

        p_matches = get_command_matches("/p")
        p_cmds = [cmd for cmd, _ in p_matches]
        self.assertIn("/provider", p_cmds)
        self.assertNotIn("/pack", p_cmds)
        self.assertNotIn("/status", p_cmds)

        xyz_matches = get_command_matches("/xyz")
        self.assertEqual(len(xyz_matches), 0)

    def test_render_dropdown_box(self):
        """Test dropdown box formatting and pointer selection."""
        box_str, num_lines, selected_cmd = render_dropdown_box("/", selected_index=0)
        self.assertIn("Commands", box_str)
        self.assertIn("❯ /run", box_str)
        self.assertEqual(selected_cmd, "/run")
        self.assertGreater(num_lines, 5)

        # Test index 1 highlights /new
        box_str2, _, selected_cmd2 = render_dropdown_box("/", selected_index=1)
        self.assertIn("❯ /new", box_str2)
        self.assertEqual(selected_cmd2, "/new")

        # Test index 2 highlights /provider
        box_str3, _, selected_cmd3 = render_dropdown_box("/", selected_index=2)
        self.assertIn("❯ /provider", box_str3)
        self.assertEqual(selected_cmd3, "/provider")

    def test_interactive_pty_down_arrow_selection(self):
        """Test live arrow key navigation in a virtual PTY."""
        master, slave = pty.openpty()
        pid = os.fork()

        if pid == 0:
            os.close(master)
            os.setsid()
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            os.close(slave)
            res = read_interactive_line()
            print(f"RESULT:{res}")
            os._exit(0)
        else:
            os.close(slave)
            import time
            time.sleep(0.05)
            # Type "/" then Down Arrow (\x1b[B) then Enter (\r)
            os.write(master, b"/\x1b[B\r")
            output = b""
            while True:
                r, _, _ = select.select([master], [], [], 1.0)
                if not r:
                    break
                try:
                    d = os.read(master, 1024)
                    if not d:
                        break
                    output += d
                    if b"RESULT:" in output:
                        break
                except OSError:
                    break
            os.close(master)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            self.assertIn(b"RESULT:/new", output)

    def test_interactive_pty_up_arrow_wrap(self):
        """Test Up Arrow wrapping from first to last command."""
        master, slave = pty.openpty()
        pid = os.fork()

        if pid == 0:
            os.close(master)
            os.setsid()
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            os.close(slave)
            res = read_interactive_line()
            print(f"RESULT:{res}")
            os._exit(0)
        else:
            os.close(slave)
            import time
            time.sleep(0.05)
            # Type "/" then Up Arrow (\x1b[A) then Enter (\r)
            os.write(master, b"/\x1b[A\r")
            output = b""
            while True:
                r, _, _ = select.select([master], [], [], 1.0)
                if not r:
                    break
                try:
                    d = os.read(master, 1024)
                    if not d:
                        break
                    output += d
                    if b"RESULT:" in output:
                        break
                except OSError:
                    break
            os.close(master)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            self.assertIn(b"RESULT:/exit", output)

    def test_interactive_pty_tab_autocomplete(self):
        """Test Tab key autocompletion."""
        master, slave = pty.openpty()
        pid = os.fork()

        if pid == 0:
            os.close(master)
            os.setsid()
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            os.close(slave)
            res = read_interactive_line()
            print(f"RESULT:{res}")
            os._exit(0)
        else:
            os.close(slave)
            import time
            time.sleep(0.05)
            # Type "/p" then Tab (\t) then Enter (\r)
            os.write(master, b"/p\t\r")
            output = b""
            while True:
                r, _, _ = select.select([master], [], [], 1.0)
                if not r:
                    break
                try:
                    d = os.read(master, 1024)
                    if not d:
                        break
                    output += d
                    if b"RESULT:" in output:
                        break
                except OSError:
                    break
            os.close(master)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            self.assertIn(b"RESULT:/provider", output)

    def test_select_from_menu_esc_cancellation(self):
        """Test that pressing Esc in select_from_menu immediately cancels without printing ^[."""
        from orchestrator.input_reader import select_from_menu
        master, slave = pty.openpty()
        pid = os.fork()

        if pid == 0:
            os.close(master)
            os.setsid()
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            os.close(slave)
            opts = [("1", "Antigravity CLI"), ("2", "Claude Code")]
            res = select_from_menu("? Pick tool:", opts)
            print(f"RESULT:{res}")
            os._exit(0)
        else:
            os.close(slave)
            import time
            time.sleep(0.05)
            # Press Esc (\x1b)
            os.write(master, b"\x1b")
            output = b""
            while True:
                r, _, _ = select.select([master], [], [], 1.0)
                if not r:
                    break
                try:
                    d = os.read(master, 1024)
                    if not d:
                        break
                    output += d
                    if b"RESULT:" in output:
                        break
                except OSError:
                    break
            os.close(master)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            self.assertIn(b"RESULT:None", output)
            self.assertNotIn(b"^[", output)

    def test_read_input_with_esc_cancellation(self):
        """Test that pressing Esc in read_input_with_esc immediately cancels."""
        from orchestrator.input_reader import read_input_with_esc
        master, slave = pty.openpty()
        pid = os.fork()

        if pid == 0:
            os.close(master)
            os.setsid()
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            os.close(slave)
            res = read_input_with_esc("Proceed? [Y/n]: ")
            print(f"RESULT:{res}")
            os._exit(0)
        else:
            os.close(slave)
            import time
            time.sleep(0.05)
            # Press Esc (\x1b)
            os.write(master, b"\x1b")
            output = b""
            while True:
                r, _, _ = select.select([master], [], [], 1.0)
                if not r:
                    break
                try:
                    d = os.read(master, 1024)
                    if not d:
                        break
                    output += d
                    if b"RESULT:" in output:
                        break
                except OSError:
                    break
            os.close(master)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            self.assertIn(b"RESULT:None", output)
            self.assertNotIn(b"^[", output)

    def test_paste_long_text_does_not_cascade_prompts(self):
        """Test that pasting text longer than terminal width does NOT duplicate prompts."""
        import fcntl
        import struct
        import termios

        master, slave = pty.openpty()
        pid = os.fork()

        if pid == 0:
            os.close(master)
            os.setsid()
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            os.close(slave)
            # Set virtual terminal size to 75 columns, 24 rows
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 75, 0, 0))
            prompt = "\033[1;36m❯\033[0m \033[1morchestrator\033[0m \033[1;33m[opencode]\033[0m › "
            res = read_interactive_line(prompt=prompt)
            print(f"RESULT:{res}")
            os._exit(0)
        else:
            os.close(slave)
            import time
            time.sleep(0.05)
            # 122 characters string that spans across multiple wrapped lines on a 75-col terminal
            long_text = "I need to generate a README file for the orchestrator repository with comprehensive installation steps and architecture."
            os.write(master, (long_text + "\r").encode("utf-8"))

            output = b""
            while True:
                r, _, _ = select.select([master], [], [], 1.0)
                if not r:
                    break
                try:
                    d = os.read(master, 4096)
                    if not d:
                        break
                    output += d
                    if b"RESULT:" in output:
                        break
                except OSError:
                    break
            os.close(master)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            # Verify prompt is never cascaded across multiple lines
            self.assertEqual(output.count(b"[opencode]"), 1)
            self.assertIn(f"RESULT:{long_text}".encode("utf-8"), output)

    def test_bracketed_paste_with_embedded_newlines(self):
        """Test that bracketed paste mode handles text with newlines without premature execution."""
        master, slave = pty.openpty()
        pid = os.fork()

        if pid == 0:
            os.close(master)
            os.setsid()
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            os.close(slave)
            prompt = "\033[1;36m❯\033[0m \033[1morchestrator\033[0m › "
            res = read_interactive_line(prompt=prompt)
            print(f"RESULT:{res}")
            os._exit(0)
        else:
            os.close(slave)
            import time
            time.sleep(0.05)
            # Bracketed paste containing multiple lines
            pasted = "\x1b[200~First line\r\nSecond line\nThird line\x1b[201~\r"
            os.write(master, pasted.encode("utf-8"))

            output = b""
            while True:
                r, _, _ = select.select([master], [], [], 1.0)
                if not r:
                    break
                try:
                    d = os.read(master, 4096)
                    if not d:
                        break
                    output += d
                    if b"RESULT:" in output:
                        break
                except OSError:
                    break
            os.close(master)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            self.assertIn(b"RESULT:First line Second line Third line", output)

    def test_read_input_with_esc_long_paste(self):
        """Test read_input_with_esc with long wrapped pasted text."""
        from orchestrator.input_reader import read_input_with_esc
        master, slave = pty.openpty()
        pid = os.fork()

        if pid == 0:
            os.close(master)
            os.setsid()
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            os.close(slave)
            res = read_input_with_esc("Goal: ")
            print(f"RESULT:{res}")
            os._exit(0)
        else:
            os.close(slave)
            import time
            time.sleep(0.05)
            long_goal = "Create a multi-agent dashboard with real-time analytics and automatic health checks for all external worker CLIs."
            os.write(master, (long_goal + "\r").encode("utf-8"))

            output = b""
            while True:
                r, _, _ = select.select([master], [], [], 1.0)
                if not r:
                    break
                try:
                    d = os.read(master, 4096)
                    if not d:
                        break
                    output += d
                    if b"RESULT:" in output:
                        break
                except OSError:
                    break
            os.close(master)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            self.assertIn(f"RESULT:{long_goal}".encode("utf-8"), output)

    def test_horizontal_cursor_movement_and_insertion(self):
        """Test moving cursor left with arrow keys and inserting character into middle of text."""
        master, slave = pty.openpty()
        pid = os.fork()

        if pid == 0:
            os.close(master)
            os.setsid()
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            os.close(slave)
            res = read_interactive_line()
            print(f"RESULT:{res}")
            os._exit(0)
        else:
            os.close(slave)
            import time
            time.sleep(0.05)
            # Type "hell world", Left Arrow 6 times, type "o", Enter
            # Cursor was at pos 10, left 6 -> pos 4 (after 'hell'), type 'o' -> 'hello world'
            seq = b"hell world\x1b[D\x1b[D\x1b[D\x1b[D\x1b[D\x1b[Do\r"
            os.write(master, seq)
            output = b""
            while True:
                r, _, _ = select.select([master], [], [], 1.0)
                if not r:
                    break
                try:
                    d = os.read(master, 1024)
                    if not d:
                        break
                    output += d
                    if b"RESULT:" in output:
                        break
                except OSError:
                    break
            os.close(master)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            self.assertIn(b"RESULT:hello world", output)

    def test_backspace_and_delete_at_cursor(self):
        """Test Backspace and Delete keys at specific cursor positions."""
        master, slave = pty.openpty()
        pid = os.fork()

        if pid == 0:
            os.close(master)
            os.setsid()
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            os.close(slave)
            res = read_interactive_line()
            print(f"RESULT:{res}")
            os._exit(0)
        else:
            os.close(slave)
            import time
            time.sleep(0.05)
            # Type "heXllo", Left Arrow 3 times (after 'X'), Backspace (deletes 'X'), Enter
            seq = b"heXllo\x1b[D\x1b[D\x1b[D\x7f\r"
            os.write(master, seq)
            output = b""
            while True:
                r, _, _ = select.select([master], [], [], 1.0)
                if not r:
                    break
                try:
                    d = os.read(master, 1024)
                    if not d:
                        break
                    output += d
                    if b"RESULT:" in output:
                        break
                except OSError:
                    break
            os.close(master)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            self.assertIn(b"RESULT:hello", output)

    def test_up_arrow_command_history_recall(self):
        """Test recalling previously executed commands using Up Arrow."""
        from orchestrator.input_reader import SESSION_HISTORY
        SESSION_HISTORY.clear()
        SESSION_HISTORY.append("echo first command")
        SESSION_HISTORY.append("git status -s")

        master, slave = pty.openpty()
        pid = os.fork()

        if pid == 0:
            os.close(master)
            os.setsid()
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            os.close(slave)
            res = read_interactive_line()
            print(f"RESULT:{res}")
            os._exit(0)
        else:
            os.close(slave)
            import time
            time.sleep(0.05)
            # Press Up Arrow (\x1b[A) to recall "git status -s", then Enter (\r)
            os.write(master, b"\x1b[A\r")
            output = b""
            while True:
                r, _, _ = select.select([master], [], [], 1.0)
                if not r:
                    break
                try:
                    d = os.read(master, 1024)
                    if not d:
                        break
                    output += d
                    if b"RESULT:" in output:
                        break
                except OSError:
                    break
            os.close(master)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            self.assertIn(b"RESULT:git status -s", output)


if __name__ == "__main__":
    unittest.main()

