"""QA Role Pack: Test strategy, edge case audit, test execution, and release sign-off."""

from typing import List
from orchestrator.packs.business_idea import PackTaskSpec


class QAPack:
    """Pre-configured workflow for software quality assurance, testing, and release sign-off."""

    NAME = "qa"
    DESCRIPTION = "Designs test matrices, identifies edge cases, crafts test suites, and assesses release readiness."

    TASKS = [
        PackTaskSpec(
            task_id="1",
            title="Test Strategy & Coverage Matrix",
            role="planner",
            prompt_template=(
                "You are a QA Lead and Test Architect.\n"
                "System under test / Feature goal: {goal}\n\n"
                "Formulate a complete Test Strategy and Coverage Matrix:\n"
                "1. Scope of Testing (Unit, Integration, Functional, Regression)\n"
                "2. Critical User Flows & Happy Path Scenarios\n"
                "3. Performance, Load & Latency Baselines\n"
                "4. Environmental & Platform Matrix (OS, browsers, dependencies)\n"
                "5. Pass/Fail Exit Criteria"
            )
        ),
        PackTaskSpec(
            task_id="2",
            title="Edge Case & Vulnerability Analysis",
            role="critic",
            prompt_template=(
                "You are an Adversarial Chaos and Reliability Tester.\n"
                "Goal: {goal}\n"
                "Test Strategy:\n{previous_output}\n\n"
                "Uncover critical failure scenarios and boundary gaps:\n"
                "- Malformed, oversized, or empty inputs\n"
                "- Network dropouts, high latency, and timeouts\n"
                "- Concurrency collisions, idempotency failures, and deadlocks\n"
                "- Corrupt state and recovery behaviors\n"
                "List top failure points with reproduction steps."
            )
        ),
        PackTaskSpec(
            task_id="3",
            title="Automated Test Suite & Verification Scripts",
            role="coder",
            prompt_template=(
                "You are a Senior Test Automation Engineer.\n"
                "Goal: {goal}\n"
                "Strategy & Edge Cases:\n{previous_output}\n\n"
                "Generate executable test cases:\n"
                "1. Automated test code (e.g. pytest, unittest, or integration scripts)\n"
                "2. Mock fixtures, test data generators, and boundary inputs\n"
                "3. Assertion matrix with clear failure messages\n"
                "4. Instructions to run the tests locally in CI/CD"
            )
        ),
        PackTaskSpec(
            task_id="4",
            title="Release Readiness & Quality Sign-Off",
            role="reviewer",
            prompt_template=(
                "You are the Independent Release Quality Reviewer.\n"
                "Goal: {goal}\n"
                "Test Artifacts:\n{previous_output}\n\n"
                "Evaluate release readiness:\n"
                "- Are critical edge cases adequately guarded by tests?\n"
                "- Is the exit criteria met with acceptable risk tolerance?\n\n"
                "Output format:\n"
                "Status: [APPROVED / REVISE]\n"
                "Release Recommendation: [GO / NO-GO] with 2-sentence rationale."
            )
        )
    ]

    @classmethod
    def get_tasks(cls, goal: str) -> List[PackTaskSpec]:
        return cls.TASKS
