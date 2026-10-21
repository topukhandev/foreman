"""Tests verifying UI & UX improvements from orchestrator_ui_ux_evaluation.md."""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from orchestrator.config import OrchestratorConfig
from orchestrator.adapters.base import BaseCLIAdapter, ExecutionResult
from orchestrator.repl import InteractiveShell
from orchestrator.terminal import send_desktop_notification


class TestEvaluationUXImprovements(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_eval_ux_")
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
""")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_status_bar_rendering(self):
        """Test persistent single-line status bar contains provider, RAM, CPU, workers, and visual mode."""
        shell = InteractiveShell(project_dir=self.test_dir)
        bar = shell._render_status_bar()
        self.assertIn("Provider:", bar)
        self.assertIn("RAM:", bar)
        self.assertIn("CPU:", bar)
        self.assertIn("Workers:", bar)
        self.assertIn("Visual:", bar)

    def test_stream_output_in_adapter(self):
        """Test BaseCLIAdapter.run with stream_output=True."""
        adapter = BaseCLIAdapter(name="test_stream", base_command="echo 'Streaming token line'")
        adapter.visual_mode = False
        res = adapter.run(prompt="", cwd=self.test_dir, stream_output=True)
        self.assertTrue(res.success)
        self.assertIn("Streaming token line", res.output)

    def test_send_desktop_notification_safe_fallback(self):
        """Test send_desktop_notification runs safely without raising exceptions."""
        try:
            send_desktop_notification("Test Alert", "This is a test notification", urgency="normal")
        except Exception as e:
            self.fail(f"send_desktop_notification raised exception: {e}")

    @patch("orchestrator.repl.select_from_menu")
    def test_interactive_tasks_menu_navigation(self, mock_select):
        """Test /tasks uses select_from_menu for arrow-key navigation."""
        shell = InteractiveShell(project_dir=self.test_dir)
        shell.hub.update_task_status(
            task_id="task-99",
            title="Build Auth API",
            worker="mock",
            state="done",
            one_line_summary="JWT authentication ready",
            role="coder"
        )
        shell.hub.save_task_spec(
            task_id="task-99",
            title="Build Auth API",
            prompt="Task #task-99\n\n## Checklist\n- [x] JWT endpoints",
            role="coder"
        )

        mock_select.return_value = "task-99"
        shell.handle_tasks_menu()

        mock_select.assert_called_once()
        args, kwargs = mock_select.call_args
        # Verify the options include task-99 and title
        options = args[1]
        self.assertEqual(options[0][0], "task-99")
        self.assertIn("Build Auth API", options[0][1])


if __name__ == "__main__":
    unittest.main()
