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
            "\033[90m────────────────────────────────────────────────────────────────────────\033[0m",
            f" \033[1;36m🎛️  FOREMAN PROGRESS\033[0m \033[90m|\033[0m Time: \033[1;37m{now_str}\033[0m \033[90m|\033[0m Elapsed: \033[1;33m{elapsed_str}\033[0m",
            f" \033[1mGoal\033[0m: {self.goal or 'General Orchestration'}",
            "\033[90m────────────────────────────────────────────────────────────────────────\033[0m"
        ]

        if active_count > 0:
            lines.append(f" \033[1;36m▶ Active Workers ({active_count}):\033[0m")
            for agent in self.active_agents.values():
                sess = f" ({agent.session_id})" if agent.session_id else ""
                running_sec = int(agent.running_seconds)
                lines.append(
                    f"   \033[1;33m⏳\033[0m \033[1;37m[{agent.role.capitalize()}]\033[0m {agent.worker_provider}{sess} "
                    f"→ Task #{agent.task_id}: {agent.task_title} ({running_sec}s)"
                )
        else:
            lines.append(" \033[90m(No workers currently running)\033[0m")

        lines.append("")
        total_tasks = len(self.tasks)
        done_tasks = sum(1 for t in self.tasks.values() if t.status == "done")
        lines.append(f" \033[1;37mTask Progress ({done_tasks}/{total_tasks} Completed):\033[0m")

        for t in self.tasks.values():
            if t.status == "done":
                status_icon = "\033[1;32m✓\033[0m"
                status_label = "\033[1;32mDone\033[0m"
            elif t.status == "running":
                status_icon = "\033[1;36m▶\033[0m"
                status_label = "\033[1;36mRunning\033[0m"
            elif t.status == "reviewing":
                status_icon = "\033[1;35m🔍\033[0m"
                status_label = "\033[1;35mReviewing\033[0m"
            elif t.status == "revising":
                status_icon = "\033[1;33m↺\033[0m"
                status_label = f"\033[1;33mRevising (Bounce {t.bounces}/3)\033[0m"
            elif t.status == "failed":
                status_icon = "\033[1;31m✗\033[0m"
                status_label = "\033[1;31mFailed\033[0m"
            elif t.status == "blocked":
                status_icon = "\033[1;31m⊘\033[0m"
                status_label = "\033[1;31mBlocked\033[0m"
            else:
                status_icon = "\033[90m○\033[0m"
                status_label = "\033[90mPending\033[0m"

            duration = int(t.duration_seconds)
            dur_str = f"({duration}s)" if t.started_at else ""

            # Clean markdown artifacts from summary
            clean_summary = ""
            if t.summary:
                import re
                s = re.sub(r"^[#*`\s-]+", "", t.summary).strip()
                s_first = s.splitlines()[0] if s else ""
                if len(s_first) > 55:
                    s_first = s_first[:52] + "..."
                if s_first:
                    clean_summary = f" \033[90m─ {s_first}\033[0m"

            lines.append(
                f"   {status_icon} \033[1;37mTask #{t.task_id}\033[0m: {t.title} "
                f"\033[36m[{t.role}]\033[0m ─ {status_label} {dur_str}{clean_summary}"
            )

        lines.append("\033[90m────────────────────────────────────────────────────────────────────────\033[0m")
        return "\n".join(lines)
