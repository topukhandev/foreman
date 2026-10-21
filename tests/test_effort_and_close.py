"""Tests verifying /effort reasoning levels and instant terminal closing."""

import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

from orchestrator.config import load_config, OrchestratorConfig
from orchestrator.adapters.antigravity import AntigravityAdapter
from orchestrator.adapters.claude import ClaudeCodeAdapter
from orchestrator.adapters.opencode import OpenCodeAdapter
from orchestrator.adapters import create_adapter
from orchestrator.repl import InteractiveShell


class TestEffortAndInstantClose(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_effort_")
        self.config_path = os.path.join(self.test_dir, "providers.yaml")
        with open(self.config_path, "w") as f:
            f.write("""
effort: medium
providers:
  antigravity_cli:
    enabled: true
    roles: [coder]
    command: "agy -p"
  claude_code:
    enabled: true
    roles: [coder]
    command: "claude -p"
  opencode:
    enabled: true
    roles: [coder]
    command: "opencode run"
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
""")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_config_effort_parsing(self):
        """Test config loads effort level properly."""
        cfg = load_config(self.config_path)
        self.assertEqual(cfg.effort, "medium")

    def test_antigravity_effort_flag(self):
        """Test AntigravityAdapter passes --effort flag."""
        adapter_low = AntigravityAdapter(effort="low")
        cmd_low = adapter_low.build_command("test")
        self.assertIn("--effort=low", cmd_low)

        adapter_high = AntigravityAdapter(effort="high")
        cmd_high = adapter_high.build_command("test")
        self.assertIn("--effort=high", cmd_high)

    def test_claude_effort_model_mapping(self):
        """Test ClaudeCodeAdapter maps low to haiku and high to sonnet-3.7."""
        adapter_low = ClaudeCodeAdapter(effort="low")
        cmd_low = adapter_low.build_command("test")
        self.assertIn("--model", cmd_low)
        self.assertIn("claude-3-5-haiku", cmd_low)

        adapter_high = ClaudeCodeAdapter(effort="high")
        cmd_high = adapter_high.build_command("test")
        self.assertIn("--model", cmd_high)
        self.assertIn("claude-3-7-sonnet", cmd_high)

    def test_opencode_effort_thinking_flag(self):
        """Test OpenCodeAdapter adds --thinking on high effort."""
        adapter_high = OpenCodeAdapter(effort="high")
        cmd_high = adapter_high.build_command("test")
        self.assertIn("--thinking", cmd_high)

    def test_repl_handle_effort_menu_direct(self):
        """Test switching effort level via /effort in REPL."""
        shell = InteractiveShell(project_dir=self.test_dir)
        self.assertEqual(shell.effort, "medium")

        shell.handle_effort_menu("high")
        self.assertEqual(shell.effort, "high")

        # Verify persisted in providers.yaml
        cfg = load_config(self.config_path)
        self.assertEqual(cfg.effort, "high")

        # Check status bar renders effort
        bar = shell._render_status_bar()
        self.assertIn("Effort:", bar)
        self.assertIn("HIGH", bar)

    @patch("orchestrator.terminal.close_all_visual_sessions")
    @patch("orchestrator.terminal.close_all_spawned_terminals")
    def test_instant_terminal_closing(self, mock_close_spawned, mock_close_visual):
        """Test handle_close_terminals runs instantly in under 0.5s."""
        shell = InteractiveShell(project_dir=self.test_dir)
        start_t = time.time()
        shell.handle_close_terminals()
        elapsed = time.time() - start_t

        mock_close_visual.assert_called_once()
        mock_close_spawned.assert_called_once()
        self.assertLess(elapsed, 0.5)
        self.assertIsNotNone(shell.last_duration_sec)

    @patch("orchestrator.terminal.close_all_visual_sessions")
    @patch("orchestrator.terminal.close_all_spawned_terminals")
    def test_lightweight_query_intercepts_close_terminals(self, mock_close_spawned, mock_close_visual):
        """Test natural language queries for closing terminals are intercepted locally without calling AI."""
        shell = InteractiveShell(project_dir=self.test_dir)

        # English
        handled = shell.handle_lightweight_query("close the terminals")
        self.assertTrue(handled)
        mock_close_visual.assert_called_once()

        # Bengali
        handled_bn = shell.handle_lightweight_query("সব টার্মিনাল বন্ধ করো")
        self.assertTrue(handled_bn)


if __name__ == "__main__":
    unittest.main()
