"""File-based Hub protocol with atomic writes and corruption recovery."""

from dataclasses import asdict, dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Dict, List, Optional


@dataclass
class InboxMessage:
    msg_id: str
    sender: str
    recipient: str
    task_id: str
    msg_type: str  # assign | result | review | revise | question
    body: str
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()


@dataclass
class TaskStatus:
    task_id: str
    title: str
    worker: str
    state: str  # pending | running | reviewing | revising | done | failed | blocked
    one_line_summary: str
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    duration_sec: float = 0.0
    role: str = "coder"
    bounces: int = 0


class Hub:
    """Manages file-based coordination hub with crash-proof atomic writes."""

    def __init__(self, root_dir: str):
        self.root_dir = Path(root_dir)
        self.hub_dir = self.root_dir / "hub"
        self.inbox_dir = self.hub_dir / "inbox"
        self.tasks_dir = self.hub_dir / "tasks"
        self.status_dir = self.hub_dir / "status"
        self.logs_dir = self.hub_dir / "logs"
        self.archive_dir = self.hub_dir / "archive"
        self.state_file = self.hub_dir / "state.md"

        self.init_structure()

    def init_structure(self) -> None:
        """Create hub directories and initial state.md if not present."""
        for d in [self.inbox_dir, self.tasks_dir, self.status_dir, self.logs_dir, self.archive_dir]:
            d.mkdir(parents=True, exist_ok=True)

        if not self.state_file.exists():
            self.write_state(
                goal="No goal set yet.",
                phase="idle",
                active_tasks=[],
                blockers=[]
            )

    def _atomic_write_text(self, target_path: Path, content: str) -> None:
        """Atomically write text file using temporary file and atomic rename."""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_fd, temp_path = tempfile.mkstemp(dir=str(target_path.parent), prefix=".tmp_")
        with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(temp_path, str(target_path))

    def _atomic_write_json(self, target_path: Path, data: Any) -> None:
        """Atomically write JSON data."""
        self._atomic_write_text(target_path, json.dumps(data, indent=2))

    def send_message(self, message: InboxMessage) -> str:
        """Write a message file to the inbox atomically."""
        ts = int(time.time() * 1000)
        filename = f"{ts}_{message.task_id}_{message.msg_type}.json"
        msg_path = self.inbox_dir / filename
        self._atomic_write_json(msg_path, asdict(message))
        return str(msg_path)

    def get_messages(self, recipient: Optional[str] = None, task_id: Optional[str] = None) -> List[InboxMessage]:
        """Read all matching inbox messages sorted chronologically, ignoring corrupted files."""
        messages: List[InboxMessage] = []
        if not self.inbox_dir.exists():
            return messages

        files = sorted(self.inbox_dir.glob("*.json"))
        for f in files:
            try:
                with open(f, "r", encoding="utf-8") as mf:
                    data = json.load(mf)
                    msg = InboxMessage(**data)
                    if recipient and msg.recipient != recipient and recipient != "*":
                        continue
                    if task_id and msg.task_id != task_id:
                        continue
                    messages.append(msg)
            except Exception:
                # Corrupted or locked message file - skip gracefully
                continue
        return messages

    def save_task_spec(self, task_id: str, title: str, prompt: str, role: str = "coder", context: str = "") -> str:
        """Create or update tasks/<task_id>.md atomically."""
        task_file = self.tasks_dir / f"{task_id}.md"
        content = (
            f"# Task {task_id}: {title}\n\n"
            f"- **Role**: {role}\n"
            f"- **Created**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"## Context & Input\n{context or 'None provided.'}\n\n"
            f"## Instructions\n{prompt}\n\n"
            f"## Status\nPending execution.\n"
        )
        self._atomic_write_text(task_file, content)
        return str(task_file)

    def append_task_result(self, task_id: str, section_title: str, content: str) -> None:
        """Append worker result or review notes to task file."""
        task_file = self.tasks_dir / f"{task_id}.md"
        if task_file.exists():
            with open(task_file, "a", encoding="utf-8") as f:
                f.write(f"\n## {section_title} ({datetime.now().strftime('%H:%M:%S')})\n{content}\n")

    def read_task_spec(self, task_id: str) -> str:
        """Read content of task file."""
        task_file = self.tasks_dir / f"{task_id}.md"
        if task_file.exists():
            try:
                with open(task_file, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                return ""
        return ""

    def update_task_status(
        self,
        task_id: str,
        title: str,
        worker: str,
        state: str,
        one_line_summary: str,
        role: str = "coder",
        started_at: Optional[float] = None,
        completed_at: Optional[float] = None,
        bounces: int = 0
    ) -> None:
        """Write status/<task_id>.json atomically."""
        status_file = self.status_dir / f"{task_id}.json"
        duration = 0.0
        if started_at:
            end_t = completed_at if completed_at else time.time()
            duration = max(0.0, end_t - started_at)

        stat = TaskStatus(
            task_id=task_id,
            title=title,
            worker=worker,
            state=state,
            one_line_summary=one_line_summary,
            started_at=started_at,
            completed_at=completed_at,
            duration_sec=duration,
            role=role,
            bounces=bounces
        )
        self._atomic_write_json(status_file, asdict(stat))

    def get_all_task_statuses(self) -> List[TaskStatus]:
        """Load all task statuses safely."""
        statuses: List[TaskStatus] = []
        if not self.status_dir.exists():
            return statuses
        for sf in sorted(self.status_dir.glob("*.json")):
            try:
                with open(sf, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    statuses.append(TaskStatus(**data))
            except Exception:
                continue
        return statuses

    def append_log(self, task_id: str, text: str) -> None:
        """Append raw output to logs/<task_id>.log with timestamp."""
        log_file = self.logs_dir / f"{task_id}.log"
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
        except Exception:
            pass

    def archive_task(self, task_id: str, summary: str) -> None:
        """Summarize finished task into archive/ and append to archive index."""
        archive_file = self.archive_dir / f"{task_id}.txt"
        summary_line = f"{task_id}: {summary} ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})\n"
        self._atomic_write_text(archive_file, summary_line)

        master_archive = self.archive_dir / "index.md"
        with open(master_archive, "a", encoding="utf-8") as f:
            f.write(f"- **{task_id}**: {summary} *({datetime.now().strftime('%H:%M:%S')})*\n")

    def get_current_goal(self) -> str:
        """Extract goal string from state.md if present."""
        content = self.read_state()
        for line in content.splitlines():
            if line.startswith("**Goal**:"):
                return line.replace("**Goal**:", "").strip()
        return "Idle / Front-Desk Chat"

    def cleanup_stale_active_tasks(self, default_provider: Optional[str] = None) -> bool:
        """
        Detect and clear any orphaned 'running' tasks left behind by previous interrupted sessions.
        Ensures hub/state.md always accurately reflects idle status when no sprint is executing.
        """
        content = self.read_state()
        if not content:
            return False

        has_stale_running = ("| running |" in content or "**Current Phase**: `active`" in content)
        if has_stale_running:
            # Mark any stale task status files as interrupted
            if self.status_dir.exists():
                for sf in self.status_dir.glob("*.json"):
                    try:
                        with open(sf, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        if data.get("state") in ("running", "reviewing", "revising"):
                            data["state"] = "interrupted"
                            data["one_line_summary"] = "Task was interrupted by session exit / server restart."
                            self._atomic_write_json(sf, data)
                    except Exception:
                        pass

            goal = self.get_current_goal()
            self.write_state(
                goal=goal,
                phase="idle",
                active_tasks=[],
                blockers=[],
                default_provider=default_provider
            )
            return True
        return False

    def write_state(
        self,
        goal: str,
        phase: str,
        active_tasks: List[Dict[str, Any]],
        blockers: List[str],
        completed_tasks: Optional[List[str]] = None,
        default_provider: Optional[str] = None
    ) -> None:
        """
        Write one-page state.md atomically.
        Strictly guarantees single-page context economy for manager agent.
        """
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "# Project State (Foreman Hub)",
            f"*Last Updated: {now_str}*",
            ""
        ]

        if default_provider:
            lines.append(f"**Primary AI Provider**: `{default_provider}`")

        lines.extend([
            f"**Goal**: {goal}",
            f"**Current Phase**: `{phase}`",
            ""
        ])

        # Active Tasks Table
        lines.append("## Active Tasks & Workers")
        if active_tasks:
            lines.append("| Task ID | Title | Role | Worker | Running Time | Status |")
            lines.append("|---------|-------|------|--------|--------------|--------|")
            for t in active_tasks:
                running_time = f"{int(t.get('running_sec', 0))}s"
                lines.append(
                    f"| {t.get('id', '')} | {t.get('title', '')} | {t.get('role', '')} | "
                    f"`{t.get('worker', '')}` | {running_time} | {t.get('status', '')} |"
                )
        else:
            lines.append("_No tasks currently active._")
        lines.append("")

        # Blockers
        lines.append("## Blockers / Manual Actions")
        if blockers:
            for b in blockers:
                lines.append(f"- ⚠️ **ACTION REQUIRED**: {b}")
        else:
            lines.append("_None (All systems nominal)_")
        lines.append("")

        # Recent Completed Summaries (strictly last 5 to guarantee single page)
        if completed_tasks:
            lines.append("## Completed Tasks")
            for c in completed_tasks[-5:]:
                lines.append(f"- [x] {c}")
            if len(completed_tasks) > 5:
                lines.append(f"*(and {len(completed_tasks) - 5} earlier tasks in hub/archive/)*")
            lines.append("")

        self._atomic_write_text(self.state_file, "\n".join(lines))

    def read_state(self) -> str:
        """Read state.md content."""
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                return ""
        return ""
