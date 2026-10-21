"""Tests for Claude Code CLI-inspired UI/UX refinements across Orchestrator."""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from orchestrator.repl import (
    InteractiveShell,
    clean_user_facing_output,
    print_claude_error,
    extract_spawn_directives
)
from orchestrator.config import load_config


class TestClaudeUIUX(unittest.TestCase):
    """Test suite for Claude Code styled terminal interface, banners, action cards, and output sanitation."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_claude_ui_")
        self.config_path = os.path.join(self.test_dir, "providers.yaml")
        with open(self.config_path, "w") as f:
            f.write("""
providers:
  mock:
    enabled: true
    roles: [planner, coder, reviewer]
    command: "mock"
routing:
  default: mock
budget:
  max_runs_per_day: 50
  max_parallel: 2
  max_time_per_task_sec: 10
  loop_detect_threshold: 3
visual_terminal: false
effort: medium
""")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_clean_user_facing_output_strips_spawn_syntax(self):
        """Verify internal /spawn commands are stripped from final user-facing text."""
        raw_text = (
            "I have located your AI projects directory and prepared the environment.\n"
            "/spawn opencode /home/user/projects/AI-Projects\n"
            "You can now begin pair programming."
        )
        cleaned = clean_user_facing_output(raw_text)
        self.assertNotIn("/spawn", cleaned)
        self.assertIn("I have located your AI projects directory", cleaned)
        self.assertIn("You can now begin pair programming.", cleaned)

    def test_clean_user_facing_output_strips_spawn_code_blocks(self):
        """Verify markdown code blocks containing only /spawn commands are stripped."""
        raw_text = (
            "Found your repository at `~/Desktop/Orcastrator`.\n\n"
            "```bash\n"
            "/spawn claude ~/Desktop/Orcastrator\n"
            "```\n\n"
            "Terminal window has been opened."
        )
        cleaned = clean_user_facing_output(raw_text)
        self.assertNotIn("/spawn", cleaned)
        self.assertNotIn("```bash", cleaned)
        self.assertIn("Found your repository", cleaned)
        self.assertIn("Terminal window has been opened.", cleaned)

    def test_clean_user_facing_output_preserves_legitimate_code_blocks(self):
        """Verify normal source code blocks (Python, JS, etc.) are NOT stripped."""
        raw_text = (
            "Here is the implementation helper:\n\n"
            "```python\n"
            "def calculate_total(a, b):\n"
            "    return a + b\n"
            "```\n\n"
            "Done."
        )
        cleaned = clean_user_facing_output(raw_text)
        self.assertIn("```python", cleaned)
        self.assertIn("calculate_total", cleaned)

    def test_print_claude_error_box(self):
        """Verify print_claude_error formats error message inside rounded container."""
        from io import StringIO
        import sys
        old_stdout = sys.stdout
        try:
            sys.stdout = StringIO()
            print_claude_error("Worker failed with status 1: API rate limit exceeded")
            val = sys.stdout.getvalue()
            self.assertIn("Error", val)
            self.assertIn("API rate limit exceeded", val)
            self.assertIn("╭─", val)
            self.assertIn("╰─", val)
        finally:
            sys.stdout = old_stdout

    def test_render_banner_claude_box(self):
        """Verify render_banner produces a sleek Claude Code card with rounded borders."""
        shell = InteractiveShell(project_dir=self.test_dir)
        from io import StringIO
        import sys
        old_stdout = sys.stdout
        try:
            sys.stdout = StringIO()
            shell.render_banner()
            val = sys.stdout.getvalue()
            self.assertIn("Foreman Orchestrator", val)
            self.assertIn("cwd", val)
            self.assertIn("worker", val)
            self.assertIn("effort", val)
            self.assertIn("╭─", val)
            self.assertIn("╰─", val)
        finally:
            sys.stdout = old_stdout

    def test_status_bar_and_prompt_style(self):
        """Verify status bar maintains all required metrics while using connected Claude-style connectors."""
        shell = InteractiveShell(project_dir=self.test_dir)
        bar = shell._render_status_bar()
        self.assertIn("Provider:", bar)
        self.assertIn("Effort:", bar)
        self.assertIn("RAM:", bar)
        self.assertIn("CPU:", bar)
        self.assertIn("Workers:", bar)
        self.assertIn("Visual:", bar)
        self.assertIn("╭─", bar)

    @patch("orchestrator.terminal.spawn_terminals_batch")
    @patch("orchestrator.terminal.detect_desktop_terminal")
    @patch("orchestrator.system_guard.SystemGuard.can_spawn", return_value=(True, ""))
    def test_open_requested_terminals_claude_action_card(self, mock_can, mock_detect, mock_batch):
        """Verify open_requested_terminals outputs Claude-style tree action card."""
        from orchestrator.terminal import TerminalEmulatorInfo, SpawnedTerminal
        mock_detect.return_value = TerminalEmulatorInfo(
            name="gnome-terminal",
            executable="/bin/bash",
            args_builder=lambda t, s: ["bash", s]
        )
        mock_batch.return_value = (
            [
                SpawnedTerminal(
                    id="term1",
                    tool_name="opencode",
                    cwd="/tmp/project",
                    pid=12345,
                    title="opencode",
                    created_at=0.0,
                    session_dir="/tmp",
                    proc=None
                )
            ],
            []
        )
        shell = InteractiveShell(project_dir=self.test_dir)
        from io import StringIO
        import sys
        old_stdout = sys.stdout
        try:
            sys.stdout = StringIO()
            shell.open_requested_terminals([{"provider": "opencode", "count": 1, "dir": "/tmp/project"}])
            val = sys.stdout.getvalue()
            self.assertIn("Action:", val)
            self.assertIn("↳ Tool", val)
            self.assertIn("↳ Directory:", val)
            self.assertIn("↳ Window", val)
            self.assertIn("opencode", val)
            self.assertIn("12345", val)
            self.assertIn("Opened 1 terminal window(s)", val)
        finally:
            sys.stdout = old_stdout


if __name__ == "__main__":
    unittest.main()
