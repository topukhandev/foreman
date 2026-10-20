"""Business Idea Role Pack: Structured validation, critique, and strategy workflow."""

from dataclasses import dataclass
from typing import List


@dataclass
class PackTaskSpec:
    task_id: str
    title: str
    role: str
    prompt_template: str


class BusinessIdeaPack:
    """Pre-configured workflow for developing and validating business ideas."""

    NAME = "business_idea"
    DESCRIPTION = "Validates, stress-tests, and builds a comprehensive strategy for any business idea."

    TASKS = [
        PackTaskSpec(
            task_id="1",
            title="Comprehensive Business Checklist",
            role="planner",
            prompt_template=(
                "You are an expert Startup & Business Planner.\n"
                "The user's goal/idea is: {goal}\n\n"
                "Create a rigorous, structured checklist covering everything necessary to validate and launch:\n"
                "1. Target Customer & Problem Definition\n"
                "2. Value Proposition & Unfair Advantage\n"
                "3. Revenue Streams & Unit Economics (CAC, LTV, margins)\n"
                "4. Cost Structure (Hosting, tooling, team, marketing)\n"
                "5. Legal, Regulatory & Compliance Risks\n"
                "6. Competitor Landscape & Key Data Sources to monitor\n"
                "7. Minimum Viable Product (MVP) Scope (Must-have vs nice-to-have)\n"
                "Keep it clear, concise, and actionable."
            )
        ),
        PackTaskSpec(
            task_id="2",
            title="Critic Gap & Risk Analysis",
            role="critic",
            prompt_template=(
                "You are an adversarial Venture Critic and Risk Analyst.\n"
                "Review the business idea: {goal}\n"
                "And the planner's checklist:\n{previous_output}\n\n"
                "Find all critical flaws, weak assumptions, hidden expenses, or failure modes:\n"
                "- What fatal blind spots are in this plan?\n"
                "- What are the most likely reasons this idea fails in year 1?\n"
                "- Which assumptions must be proven true before spending money?\n"
                "List the top 3-5 risks and concrete mitigations for each."
            )
        ),
        PackTaskSpec(
            task_id="3",
            title="Actionable Strategy & Unit Economics",
            role="coder",
            prompt_template=(
                "You are an execution specialist.\n"
                "Based on the business idea '{goal}', the checklist, and the critic's risks:\n"
                "Context:\n{previous_output}\n\n"
                "Produce the final Actionable Launch Blueprint:\n"
                "1. Recommended Pricing Model & Breakeven Estimate\n"
                "2. 30-Day Go-To-Market Execution Plan (Week-by-week)\n"
                "3. Tech Stack / Tooling Recommendation for lowest overhead\n"
                "4. Day-1 Action Checklist (Immediate 3 steps to take)"
            )
        ),
        PackTaskSpec(
            task_id="4",
            title="Independent Quality & Feasibility Review",
            role="reviewer",
            prompt_template=(
                "You are an independent Quality Reviewer.\n"
                "Goal: {goal}\n"
                "Review the proposed Strategy & Launch Blueprint:\n{previous_output}\n\n"
                "Verify:\n"
                "- Are the unit economics realistic?\n"
                "- Were the critic's risks properly addressed?\n"
                "- Is the 30-day plan directly actionable by a single founder or small team?\n\n"
                "Output format:\n"
                "Status: [APPROVED / REVISE]\n"
                "Feedback / Summary: <2-3 sentences max>"
            )
        )
    ]

    @classmethod
    def get_tasks(cls, goal: str) -> List[PackTaskSpec]:
        """Return task list for the pack."""
        return cls.TASKS
