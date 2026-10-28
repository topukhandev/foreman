"""Live updates for API clients (docs/api-contract.md, "Live updates"): follow tasks' event logs from byte cursors and
say when a task should be reloaded.

Events are hints; task.json is the truth. The feed never invents events. It passes on what the runners wrote, says
"resync" when a client's cursor doesn't fit a log, and says "tasks_changed" when a task appeared or its record
changed. It also lets the engine notice a runner that died (Engine.reconcile), whose record then says so.
"""

import os
import threading
from typing import Any, Dict, List, Optional, Tuple

from orchestrator.engine import Engine

Note = Tuple[str, Dict[str, Any]]  # (notification method, params)
RECONCILE_EVERY = 8  # polls between looks for runners that died (a look takes the task's run lock briefly)


class Feed:
    def __init__(self, engine: Engine):
        self.eng = engine
        self.cursors: Dict[str, int] = {}
        self.active = False  # also follow tasks that start later
        self._known: set = set()
        self._mtimes: Dict[str, float] = {}
        self._polls = 0
        self._lock = threading.Lock()

    def _ids(self) -> List[str]:
        try:
            return [d for d in os.listdir(self.eng.store.tasks_dir) if d.isdigit()]
        except OSError:
            return []

    def _end(self, task_id: str) -> int:
        return _log_end(self.eng.store.path(task_id, "events.jsonl"))

    def subscribe(self, tasks: Any = "active", since: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
        """Follow these tasks ("active": the ones queued or running now, and every task that starts later) from the
        given cursors; a task without one is followed from the current end of its log (tasks.events has the
        history)."""
        since = {str(k): int(v) for k, v in (since or {}).items()}
        with self._lock:
            if tasks == "active":
                self.active = True
                ids = [t["id"] for t in self.eng.store.all() if t["status"] in ("queued", "planning", "running")]
            else:
                ids = [str(t) for t in tasks or []]
            ids += [i for i in since if i not in ids]
            for task_id in ids:
                self.cursors[task_id] = since.get(task_id, self._end(task_id))
                self._mtimes[task_id] = self._mtime(task_id)
            self._known = set(self._ids())
        return {}

    def unsubscribe(self) -> Dict[str, Any]:
        with self._lock:
            self.cursors.clear()
            self._mtimes.clear()
            self.active = False
        return {}

    def _mtime(self, task_id: str) -> float:
        try:
            return os.path.getmtime(self.eng.store.path(task_id, "task.json"))
        except OSError:
            return 0.0

    def poll(self) -> List[Note]:
        """What happened since the last poll, as notifications to send."""
        notes: List[Note] = []
        changed: List[str] = []
        with self._lock:
            self._polls += 1
            if self.active:
                for task_id in sorted(set(self._ids()) - self._known, key=int):  # started since: from its beginning
                    self._known.add(task_id)
                    self.cursors[task_id] = 0
                    self._mtimes[task_id] = 0.0
                    changed.append(task_id)
            for task_id, cursor in list(self.cursors.items()):
                events, nxt, resync = self.eng.store.read_events(task_id, cursor, 500)
                self.cursors[task_id] = nxt
                if resync:
                    notes.append(("resync", {"task_id": task_id, "cursor": nxt}))
                    continue
                notes += [("event", {"task_id": task_id, "cursor": e["cursor"], "event": e}) for e in events]
                mtime = self._mtime(task_id)
                if mtime != self._mtimes.get(task_id):
                    self._mtimes[task_id] = mtime
                    if not events and task_id not in changed:
                        changed.append(task_id)  # the record changed without an event saying so
            look = self._polls % RECONCILE_EVERY == 0
            ids = list(self.cursors)
        if look:  # outside the feed's lock: reconciling takes a task's run lock
            for task_id in ids:
                task = self.eng.store.load(task_id)
                if task and task["status"] in ("running", "planning", "queued"):
                    self.eng.reconcile(task)  # its status event arrives with the next poll
        if changed:
            notes.append(("tasks_changed", {"task_ids": changed}))
        return notes


def _log_end(path: str) -> int:
    """The cursor just after the last complete line of a log (where following it from now starts)."""
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            while size:  # read backwards from the end until a line break: logs only grow
                start = max(0, size - 65536)
                f.seek(start)
                i = f.read(size - start).rfind(b"\n")
                if i >= 0:
                    return start + i + 1
                size = start
    except OSError:
        pass
    return 0
