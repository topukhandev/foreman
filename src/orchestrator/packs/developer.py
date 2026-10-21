"""Developer Role Pack: Codebase analysis, adversarial review, implementation, and quality sign-off."""

from typing import List
from orchestrator.packs.business_idea import PackTaskSpec


class DeveloperPack:
    """Pre-configured workflow for software development, refactoring, and feature implementation."""

    NAME = "developer"
    DESCRIPTION = "Architects, audits, implements, and independently verifies software features and fixes."

    TASKS = [
        PackTaskSpec(
            task_id="1",
            title="Architecture & Impact Analysis",
            role="planner",
            prompt_template=(
                "You are a Lead Software Architect.\n"
                "The engineering objective is: {goal}\n\n"
                "Produce a rigorous technical design specification:\n"
                "1. High-level Architecture & Component Diagram description\n"
                "2. Affected files, modules, and API boundaries\n"
                "3. Dependencies & Data Flow\n"
                "4. Performance, Scalability & Security Implications\n"
                "5. Step-by-step Implementation Order\n"
                "Be technically precise and concise."
            )
        ),
        PackTaskSpec(
            task_id="2",
            title="Adversarial Code & Security Audit",
            role="critic",
            prompt_template=(
                "You are an Adversarial Security Engineer and Code Reviewer.\n"
                "Objective: {goal}\n"
                "Architect's Plan:\n{previous_output}\n\n"
                "Find critical vulnerabilities, anti-patterns, and race conditions:\n"
                "- Injection risks, auth bypasses, or data exposure\n"
                "- Concurrency issues, resource leaks, or unhandled exceptions\n"
                "- Breaking changes or backward incompatibility\n"
                "List findings by severity (High/Med/Low) with concrete code mitigations."
            )
        ),
        PackTaskSpec(
            task_id="3",
            title="Implementation Details & Test Suite",
            role="coder",
            prompt_template=(
                "You are a Senior Implementation Engineer.\n"
                "Objective: {goal}\n"
                "Incorporate the architectural plan and security mitigations:\n{previous_output}\n\n"
                "Provide:\n"
                "1. Implement the solution: create all necessary source code files, data models, and assets directly on disk in the project directory.\n"
                "2. Robust error handling, validation, and modular architecture.\n"
                "3. Write comprehensive unit test cases and run them to verify they pass.\n"
                "4. Deliver an implementation report summarizing all created files, test execution results, and instructions to run the application."
            )
        ),
        PackTaskSpec(
            task_id="4",
            title="Independent Verification & Code Review",
            role="reviewer",
            prompt_template=(
                "You are an Independent Principal Reviewer.\n"
                "Objective: {goal}\n"
                "Implementation:\n{previous_output}\n\n"
                "Verify:\n"
                "- Does the implementation cleanly satisfy the original requirement?\n"
                "- Are all adversarial audit findings properly resolved?\n"
                "- Is code maintainable, documented, and test-covered?\n\n"
                "Output format:\n"
                "Status: [APPROVED / REVISE]\n"
                "Review Notes: <2-3 sentences max>"
            )
        )
    ]

    @classmethod
    def get_tasks(cls, goal: str) -> List[PackTaskSpec]:
        return cls.TASKS
