import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))
"""End-to-end tests for OrchestratorEngine."""

import os
import tempfile
import unittest
from orchestrator.config import OrchestratorConfig, ProviderConfig, BudgetConfig, RoutingConfig
from orchestrator.engine import OrchestratorEngine


class TestEngine(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = OrchestratorConfig(
            machine="test-pc",
            os="linux",
            providers={
                "mock_planner": ProviderConfig(
                    name="mock_planner",
                    enabled=True,
                    verified=True,
                    command="mock",
                    roles=["planner", "coder", "researcher"]
                ),
                "mock_reviewer": ProviderConfig(
                    name="mock_reviewer",
                    enabled=True,
                    verified=True,
                    command="mock",
                    roles=["critic", "reviewer"]
                )
            },
            routing=RoutingConfig(reviewer_must_differ_from_builder=True),
            budget=BudgetConfig(max_parallel=2, max_time_per_task_sec=30)
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_run_pack_business_idea(self):
        engine = OrchestratorEngine(self.temp_dir.name, self.config)
        result = engine.run_pack("business_idea", "Autonomous Local AI Agent Marketplace")

        self.assertTrue(result.success)
        self.assertEqual(result.tasks_completed, 4)
        self.assertEqual(result.tasks_failed, 0)
        self.assertIn("Orchestration completed", result.summary_report)

        # Check state.md was written
        state_file = os.path.join(self.temp_dir.name, "hub", "state.md")
        self.assertTrue(os.path.exists(state_file))

        # Check task files and logs
        tasks_dir = os.path.join(self.temp_dir.name, "hub", "tasks")
        self.assertTrue(os.path.exists(os.path.join(tasks_dir, "1.md")))
        self.assertTrue(os.path.exists(os.path.join(tasks_dir, "4.md")))


if __name__ == "__main__":
    unittest.main()
