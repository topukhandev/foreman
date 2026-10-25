"""Daily run quotas per provider (deterministic). Warn at a threshold, hard stop at 100%."""

import os
import threading
from datetime import date
from typing import Dict, Tuple

from orchestrator.util import read_json, write_json


class Budget:
    def __init__(self, home: str, limits: Dict[str, int], warn_at_percent: int = 80):
        self.path = os.path.join(home, "budget.json")
        self.limits = limits
        self.warn_at = warn_at_percent
        self._lock = threading.Lock()

    def _ledger(self) -> Dict:
        data = read_json(self.path, {}) or {}
        if data.get("date") != date.today().isoformat():
            data = {"date": date.today().isoformat(), "runs": {}}
        return data

    def used(self, provider: str) -> int:
        return int(self._ledger()["runs"].get(provider, 0))

    def percent(self, provider: str) -> int:
        limit = self.limits.get(provider) or 0
        return int(100 * self.used(provider) / limit) if limit else 0

    def acquire(self, provider: str) -> Tuple[bool, str]:
        """Count one run. Returns (allowed, warning-or-reason)."""
        with self._lock:
            data = self._ledger()
            limit = self.limits.get(provider) or 0
            used = int(data["runs"].get(provider, 0))
            if limit and used >= limit:
                return False, f"{provider} reached its daily limit ({limit} runs)"
            data["runs"][provider] = used + 1
            write_json(self.path, data)
            if limit and (used + 1) * 100 >= limit * self.warn_at:
                return True, f"{provider} at {int(100 * (used + 1) / limit)}% of its daily limit ({used + 1}/{limit})"
            return True, ""

    def over_warning(self, provider: str) -> bool:
        limit = self.limits.get(provider) or 0
        return bool(limit) and self.used(provider) * 100 >= limit * self.warn_at
