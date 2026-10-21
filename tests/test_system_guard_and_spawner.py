"""Unit tests for System Resource Guard and multi-terminal spawner."""

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from orchestrator.config import BudgetConfig, ProviderConfig, load_config
from orchestrator.budget import BudgetGuard, BudgetExceededError
from orchestrator.system_guard import SystemGuard, SystemMetrics
from orchestrator.terminal import (
    SpawnedTerminal,
    SpawnedTerminalTracker,
    TerminalEmulatorInfo,
    spawn_terminals_batch,
    list_active_terminals,
    close_all_spawned_terminals,
)
from orchestrator.repl import InteractiveShell, parse_spawn_args, extract_spawn_directives


class TestSystemGuardAndSpawner(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="foreman_test_guard_")
        self.providers_yaml = os.path.join(self.test_dir, "providers.yaml")
        with open(self.providers_yaml, "w") as f:
            f.write("""
machine: testbox
os: linux
default_provider: antigravity_cli
providers:
  antigravity_cli:
    enabled: true
    tier: paid
    command: "agy -p"
    limits:
      max_runs_per_day: 50
      max_parallel: 10
  claude_code:
    enabled: true
    tier: paid
    command: "claude -p"
    limits:
      max_runs_per_day: 50
      max_parallel: 10
  opencode:
    enabled: true
    tier: free
    command: "opencode run"
    limits:
      max_runs_per_day: 100
      max_parallel: 10
budget:
  max_parallel: 10
""")

    def tearDown(self):
        close_all_spawned_terminals()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_system_guard_live_metrics(self):
        """Verify SystemGuard reads real hardware metrics."""
        metrics = SystemGuard.get_metrics(target_dir=self.test_dir)
        self.assertIsInstance(metrics, SystemMetrics)
        self.assertGreater(metrics.ram_total_mb, 0)
        self.assertGreater(metrics.ram_available_mb, 0)
        self.assertGreater(metrics.cpu_count, 0)
        self.assertGreater(metrics.disk_total_gb, 0)
        self.assertIn(metrics.status, ("HEALTHY", "WARNING", "CRITICAL"))

        report = SystemGuard.render_report(target_dir=self.test_dir)
        self.assertIn("SYSTEM RESOURCE & SAFETY GUARD", report)
        self.assertIn("RAM Available", report)
        self.assertIn("CPU Status", report)

    def test_system_guard_panic_prevention_on_low_memory(self):
        """Verify SystemGuard halts spawning when RAM is critically low (<400MB)."""
        mock_metrics = SystemMetrics(
            ram_total_mb=8192.0,
            ram_available_mb=250.0,  # Below 400MB critical floor!
            ram_used_percent=96.9,
            cpu_count=4,
            cpu_percent=20.0,
            load_avg_1m=1.0,
            disk_free_gb=50.0,
            disk_total_gb=100.0,
            disk_used_percent=50.0,
            is_healthy=False,
            status="CRITICAL",
            status_message="RAM critically low",
            safe_max_workers=0
        )
        with patch.object(SystemGuard, "get_metrics", return_value=mock_metrics):
            can_spawn, msg = SystemGuard.can_spawn(count=4, target_dir=self.test_dir)
            self.assertFalse(can_spawn)
            self.assertIn("Spawning halted to prevent system crash", msg)
            self.assertIn("250 MB", msg)

    def test_system_guard_cpu_overload_prevention(self):
        """Verify SystemGuard halts spawning when CPU is pinned at 98%."""
        mock_metrics = SystemMetrics(
            ram_total_mb=8192.0,
            ram_available_mb=4000.0,
            ram_used_percent=50.0,
            cpu_count=4,
            cpu_percent=98.5,  # Extreme CPU load!
            load_avg_1m=12.0,
            disk_free_gb=50.0,
            disk_total_gb=100.0,
            disk_used_percent=50.0,
            is_healthy=False,
            status="CRITICAL",
            status_message="CPU is pegged",
            safe_max_workers=0
        )
        with patch.object(SystemGuard, "get_metrics", return_value=mock_metrics):
            can_spawn, msg = SystemGuard.can_spawn(count=2, target_dir=self.test_dir)
            self.assertFalse(can_spawn)
            self.assertIn("Spawning halted", msg)

    def test_budget_guard_allows_up_to_10_workers(self):
        """Verify BudgetGuard allows spawning 4 workers without the old 2-worker barrier."""
        b_cfg = BudgetConfig(max_parallel=10)
        p_cfg = {
            "antigravity_cli": ProviderConfig(name="antigravity_cli", enabled=True, limits={"max_parallel": 10, "max_runs_per_day": 50})
        }
        guard = BudgetGuard(config=b_cfg, providers=p_cfg)

        # Acquire 4 worker slots
        for i in range(1, 5):
            allowed, warn = guard.check_and_acquire(f"task_{i}", "antigravity_cli")
            self.assertTrue(allowed, f"Worker {i} should be allowed up to 10 concurrency limit")

        self.assertEqual(len(guard.active_workers), 4)

        # Release workers
        for i in range(1, 5):
            guard.release(f"task_{i}")
        self.assertEqual(len(guard.active_workers), 0)

    def test_repl_natural_language_bridges_directly_to_ai_provider(self):
        """Verify REPL forwards user requests directly to the active AI provider without regex interception."""
        shell = InteractiveShell(project_dir=self.test_dir)
        with patch.object(shell, "bridge_to_ai_provider") as mock_bridge:
            q = "can you open 2 opencode and 1 agy terminal? opencode will be open in home dir have a projects folder on that"
            with patch("orchestrator.repl.read_interactive_line", side_effect=[q, EOFError]):
                shell.run()
            mock_bridge.assert_called_with(q)

    def test_spawn_terminals_batch_execution(self):
        """Verify batch terminal spawning with mock emulator."""
        mock_emulator = TerminalEmulatorInfo(
            name="mock-term",
            executable="/bin/bash",
            args_builder=lambda title, script: ["bash", script],
            nowait_args_builder=lambda title, script: ["bash", script]
        )

        with patch("orchestrator.terminal.detect_desktop_terminal", return_value=mock_emulator):
            with patch("subprocess.Popen") as mock_popen:
                mock_proc = MagicMock()
                mock_proc.pid = 9999
                mock_proc.poll.return_value = None
                mock_popen.return_value = mock_proc

                specs = [
                    {"provider": "antigravity_cli", "count": 2, "dir": self.test_dir},
                    {"provider": "bash", "count": 1, "dir": self.test_dir}
                ]

                spawned, errors = spawn_terminals_batch(specs)
                self.assertEqual(len(spawned), 3)
                self.assertEqual(spawned[0].tool_name, "antigravity_cli")
                self.assertEqual(spawned[1].tool_name, "antigravity_cli")
                self.assertEqual(spawned[2].tool_name, "bash")

                # Test close_all
                close_all_spawned_terminals()
                self.assertEqual(len(list_active_terminals()), 0)

    def test_repl_open_requested_terminals_natural(self):
        """Verify open_requested_terminals launches cleanly."""
        shell = InteractiveShell(project_dir=self.test_dir)
        mock_emulator = TerminalEmulatorInfo(
            name="mock-term",
            executable="/bin/bash",
            args_builder=lambda title, script: ["bash", script],
            nowait_args_builder=lambda title, script: ["bash", script]
        )
        with patch("orchestrator.terminal.detect_desktop_terminal", return_value=mock_emulator):
            with patch("subprocess.Popen") as mock_popen:
                mock_proc = MagicMock()
                mock_proc.pid = 1234
                mock_proc.poll.return_value = None
                mock_popen.return_value = mock_proc

                specs = [{"provider": "claude_code", "count": 2, "dir": self.test_dir}]
                shell.open_requested_terminals(specs)
                self.assertEqual(len(list_active_terminals()), 2)

    def test_parse_spawn_args(self):
        """Verify parse_spawn_args handles aliases, counts, and directory paths."""
        res1 = parse_spawn_args("opencode /home/user/Desktop/Orcastrator")
        self.assertIsNotNone(res1)
        self.assertEqual(res1["provider"], "opencode")
        self.assertEqual(res1["count"], 1)
        self.assertEqual(res1["dir"], "/home/user/Desktop/Orcastrator")

        res2 = parse_spawn_args(r"agy /home/user/projects/ai\ projects")
        self.assertIsNotNone(res2)
        self.assertEqual(res2["provider"], "antigravity_cli")
        self.assertEqual(res2["count"], 1)
        self.assertEqual(res2["dir"], "/home/user/projects/ai projects")

        res3 = parse_spawn_args("2 claude /tmp")
        self.assertIsNotNone(res3)
        self.assertEqual(res3["provider"], "claude_code")
        self.assertEqual(res3["count"], 2)
        self.assertEqual(res3["dir"], "/tmp")

        res4 = parse_spawn_args("3", default_provider="antigravity_cli", default_dir=self.test_dir)
        self.assertIsNotNone(res4)
        self.assertEqual(res4["provider"], "antigravity_cli")
        self.assertEqual(res4["count"], 3)
        self.assertEqual(res4["dir"], self.test_dir)

    def test_extract_spawn_directives_from_ai_markdown(self):
        """Verify extract_spawn_directives extracts directives from AI markdown output."""
        ai_response = """To open these worker sessions in their respective directories, you can launch them directly using Foreman's spawn command:

1. OpenCode Interpreter / CLI in the Desktop Orchestrator test directory:
```bash
/spawn opencode /home/topukhan/Desktop/Orcastrator
```
2. Antigravity CLI (agy) in the AI Projects directory:
```bash
/spawn agy /home/topukhan/projects/ai\\ projects
```
3. Claude Code CLI in the Desktop Image directory:
```bash
/spawn claude /home/topukhan/Desktop/image
```
*(Foreman supports up to 10-15 concurrent workers governed dynamically by the System Resource Guard. You can also run `/spawn [tool] [dir]` directly in the Foreman prompt at any time to open additional worker terminals.)*"""

        specs = extract_spawn_directives(ai_response)
        self.assertEqual(len(specs), 3)
        self.assertEqual(specs[0]["provider"], "opencode")
        self.assertEqual(specs[0]["dir"], "/home/topukhan/Desktop/Orcastrator")
        self.assertEqual(specs[1]["provider"], "antigravity_cli")
        self.assertEqual(specs[1]["dir"], "/home/topukhan/projects/ai projects")
        self.assertEqual(specs[2]["provider"], "claude_code")
        self.assertEqual(specs[2]["dir"], "/home/topukhan/Desktop/image")

    def test_bridge_to_ai_provider_auto_spawns_terminals(self):
        """Verify bridge_to_ai_provider executes emitted /spawn directives automatically without user bash prompts."""
        shell = InteractiveShell(project_dir=self.test_dir)
        from orchestrator.adapters.base import ExecutionResult
        ai_output = "Opening requested session:\n/spawn opencode /tmp"

        with patch("orchestrator.repl.create_adapter") as mock_adapter_create:
            mock_adapter = MagicMock()
            mock_adapter.run.return_value = ExecutionResult(success=True, output=ai_output)
            mock_adapter_create.return_value = mock_adapter

            with patch.object(shell, "open_requested_terminals") as mock_open:
                shell.bridge_to_ai_provider("open terminal in opencode in /tmp")
                mock_open.assert_called_once()
                specs = mock_open.call_args[0][0]
                self.assertEqual(len(specs), 1)
                self.assertEqual(specs[0]["provider"], "opencode")
                self.assertEqual(specs[0]["dir"], "/tmp")

    def test_slash_spawn_command(self):
        """Verify /spawn slash command invokes open_requested_terminals directly."""
        shell = InteractiveShell(project_dir=self.test_dir)
        with patch.object(shell, "open_requested_terminals") as mock_open:
            with patch("orchestrator.repl.read_interactive_line", side_effect=["/spawn opencode /tmp", EOFError]):
                shell.run()
            mock_open.assert_called_once()
            specs = mock_open.call_args[0][0]
            self.assertEqual(specs[0]["provider"], "opencode")
            self.assertEqual(specs[0]["dir"], "/tmp")


if __name__ == "__main__":
    unittest.main()
