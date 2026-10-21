"""Unit tests for visual desktop terminal integration."""

import os
import shutil
import tempfile
import unittest
from orchestrator.terminal import (
    detect_desktop_terminal,
    can_launch_visual_terminal,
    create_visual_runner_script,
    launch_visual_worker
)
from orchestrator.adapters.base import BaseCLIAdapter


class TestVisualTerminal(unittest.TestCase):
    """Test visual terminal detection, script generation, and execution."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_create_visual_runner_script(self):
        """Test runner script creation has correct shebang, command, PID, and exit code handling."""
        out_f = os.path.join(self.test_dir, "out.log")
        code_f = os.path.join(self.test_dir, "code.txt")
        pid_f = os.path.join(self.test_dir, "proc.pid")

        script_path = create_visual_runner_script(
            cmd=["echo", "Hello Visual Terminal"],
            cwd=self.test_dir,
            worker_name="opencode",
            task_id="1",
            role="planner",
            title="Design Architecture",
            out_path=out_f,
            code_path=code_f,
            pid_path=pid_f
        )

        self.assertTrue(os.path.exists(script_path))
        self.assertTrue(os.access(script_path, os.X_OK))

        with open(script_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("FOREMAN AI WORKER SESSION", content)
        self.assertIn("[opencode]", content)
        self.assertIn("[PLANNER] #1", content)
        self.assertIn("tee", content)
        self.assertIn("MAIN_PID=$!", content)
        self.assertIn("EXIT_CODE=$?", content)

        os.remove(script_path)

    def test_detect_desktop_terminal(self):
        """Test terminal detection in GUI environment."""
        if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            emulator = detect_desktop_terminal()
            if shutil.which("gnome-terminal") or shutil.which("x-terminal-emulator"):
                self.assertIsNotNone(emulator)
                self.assertTrue(os.path.exists(emulator.executable))

    def test_adapter_visual_mode_flag(self):
        """Test that BaseCLIAdapter respects visual_mode toggle."""
        adapter = BaseCLIAdapter("test", "echo", visual_mode=False)
        self.assertFalse(adapter.visual_mode)
        res = adapter.run("hello", cwd=self.test_dir)
        self.assertTrue(res.success)
        self.assertIn("hello", res.output)

    def test_persistent_visual_session_multiturn(self):
        """Verify that visual terminal session stays open across multiple turns and preserves context."""
        from unittest.mock import patch
        from orchestrator.terminal import (
            TerminalEmulatorInfo,
            VisualSessionManager,
            launch_visual_worker,
            close_all_visual_sessions
        )

        mock_emulator = TerminalEmulatorInfo(
            name="mock-terminal",
            executable="/bin/bash",
            args_builder=lambda title, script: ["bash", script]
        )

        with patch("orchestrator.terminal.detect_desktop_terminal", return_value=mock_emulator):
            manager = VisualSessionManager.get_instance()
            close_all_visual_sessions()

            # Turn 1: Create files
            res1 = launch_visual_worker(
                cmd=["echo", "Turn 1: architecture.md created"],
                cwd=self.test_dir,
                title="Create architecture file",
                worker_name="opencode_test",
                task_id="turn_1",
                role="worker",
                show_progress=False
            )

            self.assertIsNotNone(res1)
            self.assertTrue(res1.success)
            self.assertIn("Turn 1: architecture.md created", res1.output)

            # Assert session is STILL ALIVE and window did not close!
            session = manager.get_session("opencode_test")
            self.assertIsNotNone(session)
            self.assertTrue(session.is_alive())

            # Turn 2: Commit files in the SAME session without closing or recreating window
            res2 = launch_visual_worker(
                cmd=["echo", "Turn 2: committed to git"],
                cwd=self.test_dir,
                title="Commit files",
                worker_name="opencode_test",
                task_id="turn_2",
                role="worker",
                show_progress=False
            )

            self.assertIsNotNone(res2)
            self.assertTrue(res2.success)
            self.assertIn("Turn 2: committed to git", res2.output)

            # Assert session is STILL ALIVE after Turn 2!
            self.assertTrue(session.is_alive())

            # Now close session (as in /new or /exit)
            close_all_visual_sessions()
            self.assertFalse(session.is_alive())


if __name__ == "__main__":
    unittest.main()

