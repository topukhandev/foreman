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


if __name__ == "__main__":
    unittest.main()
