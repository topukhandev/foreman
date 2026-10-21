"""Budget Guard: Deterministic resource enforcement for AI CLI workers."""

from dataclasses import dataclass, field
from datetime import date
import json
import os
import threading
from typing import Dict, List, Optional, Tuple
from orchestrator.config import BudgetConfig, ProviderConfig


class BudgetExceededError(Exception):
    """Raised when hard budget or safety limit is breached."""
    pass


class LoopDetectedError(Exception):
    """Raised when a task bounces between builder and reviewer excessively."""
    pass


@dataclass
class BudgetStatus:
    provider_runs_today: Dict[str, int] = field(default_factory=dict)
    active_workers: int = 0
    active_tasks: List[str] = field(default_factory=list)
    bounces_by_task: Dict[str, int] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


class BudgetGuard:
    """Non-negotiable Python budget guard enforcing quotas, timeouts, and loop guards."""

    def __init__(self, config: BudgetConfig, providers: Dict[str, ProviderConfig], ledger_path: Optional[str] = None):
        self.config = config
        self.providers = providers
        self.ledger_path = ledger_path
        self._lock = threading.Lock()
        self._current_date = date.today().isoformat()

        # In-memory tracking
        self.daily_runs: Dict[str, int] = {p_name: 0 for p_name in providers.keys()}
        self.active_workers: Dict[str, str] = {}  # task_id -> provider_name
        self.task_bounces: Dict[str, int] = {}    # task_id -> bounce count
        self.warnings: List[str] = []

        self._load_ledger()

    def _load_ledger(self) -> None:
        """Load persistent ledger if available."""
        if not self.ledger_path or not os.path.exists(self.ledger_path):
            return
        try:
            with open(self.ledger_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                saved_date = data.get("date")
                if saved_date == self._current_date:
                    self.daily_runs.update(data.get("daily_runs", {}))
                self.task_bounces.update(data.get("task_bounces", {}))
        except Exception:
            # If ledger corrupted, initialize clean
            pass

    def _save_ledger(self) -> None:
        """Persist budget state to disk."""
        if not self.ledger_path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.ledger_path)), exist_ok=True)
            data = {
                "date": self._current_date,
                "daily_runs": self.daily_runs,
                "task_bounces": self.task_bounces
            }
            with open(self.ledger_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def check_and_acquire(self, task_id: str, provider_name: str) -> Tuple[bool, Optional[str]]:
        """
        Check if a task can run under budget limits.
        If allowed, records the run and active worker.
        Returns: (allowed, warning_or_error_message)
        """
        with self._lock:
            # Check rollover of date
            today_str = date.today().isoformat()
            if today_str != self._current_date:
                self._current_date = today_str
                self.daily_runs = {p: 0 for p in self.providers.keys()}

            provider = self.providers.get(provider_name)
            if not provider:
                return False, f"Unknown provider '{provider_name}'"

            # 0. System Resource Guard (RAM, CPU, and Disk Headroom)
            from orchestrator.system_guard import SystemGuard
            can_run, sys_guard_err = SystemGuard.can_spawn(1)
            if not can_run:
                return False, sys_guard_err or "System Guard: insufficient resources to spawn worker."

            # 1. Concurrency limit (Global)
            if len(self.active_workers) >= self.config.max_parallel:
                return False, f"Global concurrency limit reached ({self.config.max_parallel} active workers)"

            # 2. Concurrency limit (Provider-specific)
            provider_max_parallel = provider.limits.get("max_parallel", self.config.max_parallel)
            current_provider_active = sum(1 for p in self.active_workers.values() if p == provider_name)
            if current_provider_active >= provider_max_parallel:
                return False, f"Provider '{provider_name}' concurrency limit reached ({provider_max_parallel})"

            # 3. Daily runs quota
            max_daily = provider.limits.get("max_runs_per_day", 100)
            current_runs = self.daily_runs.get(provider_name, 0)
            if current_runs >= max_daily:
                raise BudgetExceededError(
                    f"Provider '{provider_name}' has reached its daily limit of {max_daily} runs."
                )

            # 4. Loop / bounce detection
            bounces = self.task_bounces.get(task_id, 0)
            if bounces >= self.config.loop_detect_threshold:
                raise LoopDetectedError(
                    f"Task '{task_id}' has bounced {bounces} times without passing review. Stopping runaway loop."
                )

            # Check 80% threshold warning
            warning_msg = None
            warn_threshold = int(max_daily * (self.config.warn_at_percent / 100.0))
            if current_runs + 1 >= warn_threshold and current_runs < warn_threshold + 1:
                warning_msg = (
                    f"WARNING: Provider '{provider_name}' is at {self.config.warn_at_percent}% of today's limit "
                    f"({current_runs + 1}/{max_daily} runs)."
                )
                self.warnings.append(warning_msg)

            # Acquire slot
            self.daily_runs[provider_name] = current_runs + 1
            self.active_workers[task_id] = provider_name
            self._save_ledger()

            return True, warning_msg

    def release(self, task_id: str) -> None:
        """Release active worker slot."""
        with self._lock:
            if task_id in self.active_workers:
                del self.active_workers[task_id]

    def record_bounce(self, task_id: str) -> int:
        """Record review failure bounce for a task."""
        with self._lock:
            count = self.task_bounces.get(task_id, 0) + 1
            self.task_bounces[task_id] = count
            self._save_ledger()
            return count

    def reset_bounce(self, task_id: str) -> None:
        """Reset bounce count upon task success."""
        with self._lock:
            if task_id in self.task_bounces:
                del self.task_bounces[task_id]
                self._save_ledger()

    def check_project_directory(self, project_dir: str) -> Tuple[bool, Optional[str]]:
        """Verify project file count and total size do not exceed guard limits."""
        total_files = 0
        total_size_bytes = 0
        max_bytes = self.config.max_project_size_mb * 1024 * 1024

        for root, dirs, files in os.walk(project_dir):
            # Ignore hidden VCS and cache dirs
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "node_modules" and d != "__pycache__"]
            total_files += len(files)
            for f in files:
                fp = os.path.join(root, f)
                try:
                    total_size_bytes += os.path.getsize(fp)
                except OSError:
                    pass

            if total_files > self.config.max_files_per_project:
                return False, f"Project file count ({total_files}) exceeded limit of {self.config.max_files_per_project}."
            if total_size_bytes > max_bytes:
                mb = total_size_bytes / (1024 * 1024)
                return False, f"Project size ({mb:.1f} MB) exceeded limit of {self.config.max_project_size_mb} MB."

        return True, None

    def get_status(self) -> BudgetStatus:
        """Return current snapshot of budget usage."""
        with self._lock:
            return BudgetStatus(
                provider_runs_today=dict(self.daily_runs),
                active_workers=len(self.active_workers),
                active_tasks=list(self.active_workers.keys()),
                bounces_by_task=dict(self.task_bounces),
                warnings=list(self.warnings)
            )
