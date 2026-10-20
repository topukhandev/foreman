import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))
"""Deep-dive stress testing and worst-case scenario verification suite."""

import os
import shutil
import tempfile
import time
import unittest
from orchestrator.config import OrchestratorConfig, ProviderConfig, BudgetConfig, RoutingConfig
from orchestrator.engine import OrchestratorEngine
from orchestrator.budget import BudgetGuard, BudgetExceededError, LoopDetectedError
from orchestrator.adapters.base import BaseCLIAdapter, ExecutionResult
from orchestrator.adapters.mock import MockAdapter
from orchestrator.hub import Hub, InboxMessage
from orchestrator.packs import PackTaskSpec


class RejectThenApproveAdapter(BaseCLIAdapter):
    """Simulates a reviewer that rejects the first attempt and approves the second."""

    def __init__(self, name: str = "smart_reviewer"):
        super().__init__(name=name, base_command="mock")
        self.call_count = 0

    def run(self, prompt: str, cwd: str, task_id: str = "general", session_id: str = None, extra_flags=None):
        self.call_count += 1
        if self.call_count == 1:
            return ExecutionResult(
                success=True,
                output="REVISE: Missing input validation on email parameter and error handling."
            )
        else:
            return ExecutionResult(
                success=True,
                output="APPROVED: Input validation properly implemented and verified."
            )


class AlwaysRejectAdapter(BaseCLIAdapter):
    """Simulates a reviewer that persistently rejects work (triggering loop guard)."""

    def __init__(self, name: str = "harsh_reviewer"):
        super().__init__(name=name, base_command="mock")

    def run(self, prompt: str, cwd: str, task_id: str = "general", session_id: str = None, extra_flags=None):
        return ExecutionResult(
            success=True,
            output="REVISE: The security criteria is still not met. Rejecting."
        )


class CrashingWorkerAdapter(BaseCLIAdapter):
    """Simulates a worker CLI that crashes or throws non-zero exit code."""

    def __init__(self, name: str = "crashing_worker"):
        super().__init__(name=name, base_command="mock")

    def run(self, prompt: str, cwd: str, task_id: str = "general", session_id: str = None, extra_flags=None):
        return ExecutionResult(
            success=False,
            output="",
            error="Fatal: Segmentation fault (core dumped) in worker CLI.",
            exit_code=139
        )


class TestScenariosAndResilience(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_dir = self.temp_dir.name

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_scenario_1_worker_timeout(self):
        """Worst Case 1: Worker hangs indefinitely. Must time out and terminate."""
        # Adapter with 0.3s timeout running a command that would sleep longer
        adapter = BaseCLIAdapter(name="slow_tool", base_command="python3 -c 'import time; time.sleep(5)'", timeout_sec=1)
        res = adapter.run("run prompt", cwd=self.project_dir)

        self.assertFalse(res.success)
        self.assertIn("timed out", res.error.lower())
        self.assertEqual(res.exit_code, -1)

    def test_scenario_2_reviewer_rejection_and_successful_recovery(self):
        """Worst Case 2: Reviewer rejects task on attempt 1. Worker receives feedback and succeeds on attempt 2."""
        config = OrchestratorConfig(
            machine="test-pc",
            providers={
                "mock_worker": ProviderConfig(name="mock_worker", enabled=True, verified=True, command="mock", roles=["coder"]),
                "mock_reviewer": ProviderConfig(name="mock_reviewer", enabled=True, verified=True, command="mock", roles=["reviewer"])
            },
            routing=RoutingConfig(reviewer_must_differ_from_builder=True),
            budget=BudgetConfig(max_parallel=2, loop_detect_threshold=3)
        )

        engine = OrchestratorEngine(self.project_dir, config)
        # Override reviewer with RejectThenApproveAdapter
        engine.adapters["mock_reviewer"] = RejectThenApproveAdapter()

        single_task = [
            PackTaskSpec(task_id="test_task", title="Implement Auth Endpoint", role="coder", prompt_template="Implement {goal}")
        ]

        result = engine.run_tasks("User login endpoint", single_task)

        self.assertTrue(result.success)
        self.assertEqual(result.tasks_completed, 1)
        # Check task file records both worker attempts and review notes
        task_md = engine.hub.read_task_spec("test_task")
        self.assertIn("REVISE", task_md)
        self.assertIn("APPROVED", task_md)

    def test_scenario_3_loop_threshold_stops_runaway_rejections(self):
        """Worst Case 3: Reviewer rejects endlessly. BudgetGuard must halt at bounce threshold 3."""
        config = OrchestratorConfig(
            machine="test-pc",
            providers={
                "mock_worker": ProviderConfig(name="mock_worker", enabled=True, verified=True, command="mock", roles=["coder"]),
                "mock_reviewer": ProviderConfig(name="mock_reviewer", enabled=True, verified=True, command="mock", roles=["reviewer"])
            },
            routing=RoutingConfig(reviewer_must_differ_from_builder=True),
            budget=BudgetConfig(max_parallel=2, loop_detect_threshold=3)
        )

        engine = OrchestratorEngine(self.project_dir, config)
        engine.adapters["mock_reviewer"] = AlwaysRejectAdapter()

        single_task = [
            PackTaskSpec(task_id="loop_task", title="Challenging Feature", role="coder", prompt_template="Code {goal}")
        ]

        result = engine.run_tasks("Challenging Feature", single_task)

        self.assertFalse(result.success)
        self.assertEqual(result.tasks_completed, 0)
        self.assertEqual(result.tasks_failed, 1)
        self.assertTrue(any("halted after 3 review bounces" in b for b in result.blockers))

        # Check state file records the blocker
        state = engine.hub.read_state()
        self.assertIn("halted after 3 review bounces", state)

    def test_scenario_4_worker_crash_guarantees_budget_release(self):
        """Worst Case 4: Worker process crashes unexpectedly. Budget slot must be released."""
        config = OrchestratorConfig(
            machine="test-pc",
            providers={
                "crash_prov": ProviderConfig(name="crash_prov", enabled=True, verified=True, command="mock", roles=["coder"])
            },
            routing=RoutingConfig(reviewer_must_differ_from_builder=False),
            budget=BudgetConfig(max_parallel=1)
        )

        engine = OrchestratorEngine(self.project_dir, config)
        engine.adapters["crash_prov"] = CrashingWorkerAdapter()

        single_task = [
            PackTaskSpec(task_id="crash_task", title="Buggy Run", role="coder", prompt_template="{goal}")
        ]

        result = engine.run_tasks("Trigger Crash", single_task)

        self.assertFalse(result.success)
        # Verify budget slot was cleanly released despite the crash!
        self.assertEqual(len(engine.budget.active_workers), 0)

        # Verify error log recorded
        with open(os.path.join(self.project_dir, "hub", "logs", "crash_task.log"), "r", encoding="utf-8") as f:
            log_content = f.read()
        self.assertIn("Segmentation fault", log_content)

    def test_scenario_5_corrupted_hub_files_graceful_recovery(self):
        """Worst Case 5: Partial/corrupted JSON files exist in hub. System must not crash."""
        hub = Hub(self.project_dir)

        # Write corrupted JSON to inbox
        corrupt_file = hub.inbox_dir / "corrupted_msg.json"
        with open(corrupt_file, "w") as f:
            f.write("{ incomplete json: true, ... [BROKEN")

        # Hub must read messages safely without raising JSONDecodeError
        messages = hub.get_messages()
        self.assertEqual(len(messages), 0)

        # Write valid message and ensure it is read normally
        hub.send_message(InboxMessage(
            msg_id="valid_1",
            sender="mgr",
            recipient="wkr",
            task_id="t1",
            msg_type="assign",
            body="Valid message"
        ))
        valid_messages = hub.get_messages()
        self.assertEqual(len(valid_messages), 1)

    def test_scenario_6_project_file_limit_guard(self):
        """Worst Case 6: Runaway script generates too many files. BudgetGuard halts project."""
        config = BudgetConfig(max_files_per_project=10)
        guard = BudgetGuard(config, {})

        # Create dummy project folder with 12 files
        sub_dir = os.path.join(self.project_dir, "flood")
        os.makedirs(sub_dir, exist_ok=True)
        for i in range(12):
            with open(os.path.join(sub_dir, f"file_{i}.txt"), "w") as f:
                f.write("test")

        ok, err = guard.check_project_directory(self.project_dir)
        self.assertFalse(ok)
        self.assertIn("file count", err.lower())


if __name__ == "__main__":
    unittest.main()
