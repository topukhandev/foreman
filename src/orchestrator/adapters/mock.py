"""Mock adapter for rapid deterministic testing and offline simulation."""

import time
from typing import List, Optional
from orchestrator.adapters.base import BaseCLIAdapter, ExecutionResult


class MockAdapter(BaseCLIAdapter):
    """Simulates CLI worker execution for unit tests or dry runs."""

    def __init__(self, name: str = "mock", timeout_sec: int = 10, simulated_delay: float = 0.05):
        super().__init__(name=name, base_command="mock", timeout_sec=timeout_sec)
        self.simulated_delay = simulated_delay

    def run(
        self,
        prompt: str,
        cwd: str,
        task_id: str = "general",
        session_id: Optional[str] = None,
        extra_flags: Optional[List[str]] = None,
        *args,
        **kwargs
    ) -> ExecutionResult:
        if self.simulated_delay > 0:
            time.sleep(self.simulated_delay)

        prompt_lower = prompt.lower()
        if "review" in prompt_lower or "check" in prompt_lower:
            output = (
                f"[Mock Review by {self.name} (Session {session_id or 'new'})]\n"
                f"Status: APPROVED\n"
                f"Notes: All criteria verified against requirements. Clean output with zero blockers."
            )
        elif "critic" in prompt_lower or "gap" in prompt_lower:
            output = (
                f"[Mock Gap Analysis by {self.name}]\n"
                f"- Gap 1: Legal disclaimer needed for local storage.\n"
                f"- Gap 2: Clarify rate limit response handling.\n"
                f"Verdict: Plan updated with mitigations."
            )
        elif "plan" in prompt_lower:
            output = (
                f"[Mock Planner by {self.name}]\n"
                f"1. Executive Summary & Value Proposition\n"
                f"2. Target Customer Persona & Pain Points\n"
                f"3. Revenue Model & Unit Economics\n"
                f"4. Go-To-Market & Distribution Strategy\n"
                f"5. Risks & Mitigation Matrix"
            )
        else:
            output = (
                f"[Mock Worker Output by {self.name} on task {task_id}]\n"
                f"Executed prompt: {prompt[:120]}...\n"
                f"Generated comprehensive analysis and verified locally."
            )

        return ExecutionResult(
            success=True,
            output=output,
            error=None,
            exit_code=0,
            duration_sec=self.simulated_delay
        )
