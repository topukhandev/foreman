"""Simple, clear progress tracking and console rendering for Orchestrator."""

from dataclasses import dataclass, field
from datetime import datetime
import time
from typing import Dict, List, Optional


@dataclass
class AgentActivity:
    agent_id: str
    worker_provider: str
    role: str
    task_id: str
    task_title: str
    started_at: float
    session_id: str = ""

    @property
    def running_seconds(self) -> float:
        return max(0.0, time.time() - self.started_at)


@dataclass
class TaskProgressItem:
    task_id: str
    title: str
    role: str
    status: str  # pending | running | reviewing | revising | done | failed | blocked
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    summary: str = ""
    bounces: int = 0

    @property
    def duration_seconds(self) -> float:
        if not self.started_at:
            return 0.0
        end_t = self.completed_at if self.completed_at else time.time()
        return max(0.0, end_t - self.started_at)


class ProgressTracker:
    """Tracks active agents, tasks, and elapsed runtimes simply and cleanly."""

    def __init__(self, goal: str = ""):
        self.goal = goal
        self.orchestrator_start_time = time.time()
        self.active_agents: Dict[str, AgentActivity] = {}
        self.tasks: Dict[str, TaskProgressItem] = {}

    def start_orchestrator(self, goal: str) -> None:
        self.goal = goal
        self.orchestrator_start_time = time.time()
        self.active_agents.clear()
        self.tasks.clear()

    def register_task(self, task_id: str, title: str, role: str) -> None:
        self.tasks[task_id] = TaskProgressItem(
            task_id=task_id,
            title=title,
            role=role,
            status="pending"
        )

    def agent_started(self, agent_id: str, provider: str, role: str, task_id: str, task_title: str, session_id: str = "") -> None:
        now = time.time()
        self.active_agents[agent_id] = AgentActivity(
            agent_id=agent_id,
            worker_provider=provider,
            role=role,
            task_id=task_id,
            task_title=task_title,
            started_at=now,
            session_id=session_id
        )
        if task_id in self.tasks:
            t = self.tasks[task_id]
            if t.status not in ("reviewing", "revising"):
                t.status = "running"
            if not t.started_at:
                t.started_at = now

    def agent_finished(self, agent_id: str, task_id: str, success: bool, summary: str = "") -> None:
        if agent_id in self.active_agents:
            del self.active_agents[agent_id]
        if task_id in self.tasks:
            t = self.tasks[task_id]
            if not success:
                t.status = "failed"
                t.completed_at = time.time()
                t.summary = summary

    def set_task_status(self, task_id: str, status: str, summary: str = "", bounces: Optional[int] = None) -> None:
        if task_id in self.tasks:
            self.tasks[task_id].status = status
            if summary:
                self.tasks[task_id].summary = summary
            if bounces is not None:
                self.tasks[task_id].bounces = bounces
            if status == "done":
                self.tasks[task_id].completed_at = time.time()

    def get_elapsed_formatted(self) -> str:
        elapsed = int(time.time() - self.orchestrator_start_time)
        hrs = elapsed // 3600
        mins = (elapsed % 3600) // 60
        secs = elapsed % 60
        if hrs > 0:
            return f"{hrs:02d}:{mins:02d}:{secs:02d}"
        return f"{mins:02d}:{secs:02d}"

    def render(self) -> str:
        """Render a simple, clean, trustworthy ASCII dashboard."""
        now_str = datetime.now().strftime("%H:%M:%S")
        elapsed_str = self.get_elapsed_formatted()
        active_count = len(self.active_agents)

        lines = [
            "------------------------------------------------------------------------",
            f" [ORCHESTRATOR] Current Time: {now_str} | Elapsed: {elapsed_str}",
            f" Goal: {self.goal or 'General Orchestration'}",
            "------------------------------------------------------------------------",
            f" Active AI Agents: {active_count}"
        ]

        if active_count > 0:
            for agent in self.active_agents.values():
                sess = f" ({agent.session_id})" if agent.session_id else ""
                running_sec = int(agent.running_seconds)
                lines.append(
                    f"   * [{agent.role.capitalize()}] {agent.worker_provider}{sess} "
                    f"-> Task #{agent.task_id}: {agent.task_title} (Running: {running_sec}s)"
                )
        else:
            lines.append("   (No agents currently executing)")

        lines.append("")
        total_tasks = len(self.tasks)
        done_tasks = sum(1 for t in self.tasks.values() if t.status == "done")
        lines.append(f" Task Progress ({done_tasks}/{total_tasks} Completed):")

        for t in self.tasks.values():
            status_icon = "[ ]"
            status_label = t.status.capitalize()

            if t.status == "done":
                status_icon = "[x]"
            elif t.status == "running":
                status_icon = "[>]"
            elif t.status == "reviewing":
                status_icon = "[?]"
                status_label = "Under Independent Review"
            elif t.status == "revising":
                status_icon = "[!]"
                status_label = f"Revising (Bounce {t.bounces}/3)"
            elif t.status == "failed":
                status_icon = "[X]"
            elif t.status == "blocked":
                status_icon = "[!]"
                status_label = "BLOCKED"

            duration = int(t.duration_seconds)
            dur_str = f"({duration}s)" if t.started_at else ""
            summary_str = f" - {t.summary}" if t.summary else ""

            lines.append(
                f"   {status_icon} Task #{t.task_id}: {t.title} "
                f"[{t.role}] - {status_label} {dur_str}{summary_str}"
            )

        lines.append("------------------------------------------------------------------------")
        return "\n".join(lines)
