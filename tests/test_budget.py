"""Tests for deterministic BudgetGuard."""

import os
import tempfile
import unittest
from orchestrator.config import BudgetConfig, ProviderConfig
from orchestrator.budget import BudgetGuard, BudgetExceededError, LoopDetectedError


class TestBudgetGuard(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ledger_file = os.path.join(self.temp_dir.name, "ledger.json")
        self.config = BudgetConfig(
            max_parallel=2,
            max_time_per_task_sec=60,
            warn_at_percent=80,
            loop_detect_threshold=3
        )
        self.providers = {
            "test_prov": ProviderConfig(
                name="test_prov",
                enabled=True,
                command="mock",
                limits={"max_runs_per_day": 5, "max_parallel": 2}
            )
        }
        self.guard = BudgetGuard(self.config, self.providers, self.ledger_file)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_acquire_and_release(self):
        allowed, warn = self.guard.check_and_acquire("task_1", "test_prov")
        self.assertTrue(allowed)
        self.assertEqual(len(self.guard.active_workers), 1)

        self.guard.release("task_1")
        self.assertEqual(len(self.guard.active_workers), 0)

    def test_concurrency_limit(self):
        self.guard.check_and_acquire("task_1", "test_prov")
        self.guard.check_and_acquire("task_2", "test_prov")

        # Third should be blocked due to max_parallel=2
        allowed, msg = self.guard.check_and_acquire("task_3", "test_prov")
        self.assertFalse(allowed)
        self.assertIn("concurrency limit", msg.lower())

    def test_daily_quota_and_warning(self):
        # Limit is 5 runs. 80% is 4.
        self.guard.check_and_acquire("t1", "test_prov"); self.guard.release("t1")
        self.guard.check_and_acquire("t2", "test_prov"); self.guard.release("t2")
        self.guard.check_and_acquire("t3", "test_prov"); self.guard.release("t3")

        # 4th run triggers 80% warning
        allowed, warn = self.guard.check_and_acquire("t4", "test_prov"); self.guard.release("t4")
        self.assertTrue(allowed)
        self.assertIsNotNone(warn)
        self.assertIn("80%", warn)

        # 5th run succeeds
        self.guard.check_and_acquire("t5", "test_prov"); self.guard.release("t5")

        # 6th run raises BudgetExceededError
        with self.assertRaises(BudgetExceededError):
            self.guard.check_and_acquire("t6", "test_prov")

    def test_loop_detection(self):
        self.guard.record_bounce("looping_task")
        self.guard.record_bounce("looping_task")
        self.guard.record_bounce("looping_task")  # reached threshold 3

        with self.assertRaises(LoopDetectedError):
            self.guard.check_and_acquire("looping_task", "test_prov")


if __name__ == "__main__":
    unittest.main()
