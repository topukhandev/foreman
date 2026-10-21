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
from orchestrator.repl import InteractiveShell


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

    def test_repl_natural_language_spawn_intent(self):
        """Verify REPL accurately parses Bengali and English multi-terminal requests."""
        shell = InteractiveShell(project_dir=self.test_dir)

        # Query 1: Bengali 4 terminals
        q1 = "আমি ওকে বলছিলাম চারটা টার্মিনাল ওপেন করতে"
        r1 = shell._detect_spawn_terminals_intent(q1)
        self.assertIsNotNone(r1)
        self.assertEqual(r1[0]["count"], 4)
        self.assertEqual(r1[0]["provider"], "antigravity_cli")
        self.assertEqual(r1[0]["dir"], self.test_dir)

        # Query 2: Multi-provider multi-directory Bengali instruction
        q2 = "তুমি একটা টার্মিনাল ওপেন করো হচ্ছে ক্লড দিয়ে এই ডিরেক্টরিতে, তুমি একটা টার্মিনাল ওপেন করো অ্যান্টিগ্রাভিটি দিয়ে এই ডিরেক্টরিতে, তুমি আরও দুইটা টার্মিনাল ওপেন করো ওপেন কোড দিয়ে /tmp ডিরেক্টরিতে"
        r2 = shell._detect_spawn_terminals_intent(q2)
        self.assertIsNotNone(r2)
        self.assertEqual(len(r2), 3)

        self.assertEqual(r2[0]["provider"], "claude_code")
        self.assertEqual(r2[0]["count"], 1)
        self.assertEqual(r2[0]["dir"], self.test_dir)

        self.assertEqual(r2[1]["provider"], "antigravity_cli")
        self.assertEqual(r2[1]["count"], 1)
        self.assertEqual(r2[1]["dir"], self.test_dir)

        self.assertEqual(r2[2]["provider"], "opencode")
        self.assertEqual(r2[2]["count"], 2)
        self.assertEqual(r2[2]["dir"], "/tmp")

        # Query 3: English open 4 terminals
        q3 = "open 4 terminals"
        r3 = shell._detect_spawn_terminals_intent(q3)
        self.assertIsNotNone(r3)
        self.assertEqual(r3[0]["count"], 4)

        # Query 4: Regular inquiry should return None
        q4 = "How does the system work?"
        r4 = shell._detect_spawn_terminals_intent(q4)
        self.assertIsNone(r4)

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


if __name__ == "__main__":
    unittest.main()
