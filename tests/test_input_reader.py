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
        self.assertIn("/pack", p_cmds)
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

        # Test index 1 highlights /provider
        box_str2, _, selected_cmd2 = render_dropdown_box("/", selected_index=1)
        self.assertIn("❯ /provider", box_str2)
        self.assertEqual(selected_cmd2, "/provider")

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
            self.assertIn(b"RESULT:/provider", output)

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


if __name__ == "__main__":
    unittest.main()
