"""Task store (~/.foreman/tasks/<id>/). Plain JSON + an append-only event log per task.

task.json holds the current state of a task: what was requested, which projects are involved, the plan
and step outcomes, decisions, questions, evidence (checks, diffs, reviews), what remains, and which
sessions contributed. events.jsonl is the audit trail; logs/ keeps raw worker output.

task.json is the authority; events are a log and a hint to look again. Rules that keep both reliable for every
reader (chat, MCP, desktop app):
- task.json is replaced whole (atomic) and carries `rev`, raised on every save, so a reader can tell which of two
  copies is newer. Saving a change of status, a new question or a new notice also writes an event for it.
- events.jsonl is append-only and never truncated or replaced. Each event is one line written with a single
  write; a line cut short by a crash is closed off before the next one, so it never swallows a later event.
- Readers consume complete lines only and resume from a byte offset (read_events); a line that doesn't parse is
  skipped, never fatal.
- While a task runs, only its runner (the run-lock holder) appends to its events; changes made by others take the
  run lock first (Engine._locked_change).
"""

import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from orchestrator.util import atomic_write, now_iso, read_json, redact, secret_values, short, update_json, write_json

ACTIVE = {"planning", "planned", "running", "needs_input", "blocked", "partial", "queued", "interrupted", "paused"}
FINAL = {"done", "failed", "answered", "cancelled"}


class Store:
    def __init__(self, home: str):
        self.home = home
        self.tasks_dir = os.path.join(home, "tasks")
        os.makedirs(self.tasks_dir, exist_ok=True)
        self._lock = threading.RLock()
        self._seen: Dict[str, Dict[str, Any]] = {}  # task id -> status, question count, notices as last read or saved
        # Values of secret-looking environment variables never reach task records, events or logs.
        self._secrets = secret_values()

    def clean(self, text: str) -> str:
        return redact(text, self._secrets) if self._secrets else text

    # ------------------------------------------------------------------ tasks
    def new_task(self, request: str, cwd: str, actor: str = "") -> Dict[str, Any]:
        with self._lock:
            # The task folder is the id's claim: creating it fails if another Foreman process took that number.
            counter = os.path.join(self.tasks_dir, ".counter")
            n = (read_json(counter, 0) or 0) + 1
            while True:
                try:
                    os.mkdir(self.path(str(n)))
                    break
                except FileExistsError:
                    n += 1
            write_json(counter, n)
            task = {
                "id": str(n), "title": short(request, 70), "request": request, "cwd": cwd,
                "status": "planning", "created_at": now_iso(), "updated_at": now_iso(),
                "intent": None, "projects": {}, "steps": [], "checks": [], "decisions": [],
                "questions": [], "evidence": [], "remaining": [], "sessions": [], "notices": [],
                "suggestions": [], "answer": None, "outcome": None, "summary": "", "replans": 0,
                "fix_rounds": 0, "runs": 0,
            }
            self.save(task)
            self.event(task["id"], "created", request=request, cwd=cwd, **({"actor": actor} if actor else {}))
            return task

    def path(self, task_id: str, *parts: str) -> str:
        return os.path.join(self.tasks_dir, task_id, *parts)

    def save(self, task: Dict[str, Any], reason: str = "") -> None:
        """Replace the task record. `reason` explains a change of status in its event (e.g. runner_exited)."""
        with self._lock:
            task["updated_at"] = now_iso()
            for i, q in enumerate(task.get("questions") or [], 1):  # append-only list: the position is a stable id
                q.setdefault("id", f"q{i}")
            before = self._seen.get(task["id"])
            if before is None and os.path.exists(self.path(task["id"], "task.json")):
                before = self._remember(self.load(task["id"]) or {})
            task["rev"] = max(int(task.get("rev") or 0), int((before or {}).get("rev") or 0)) + 1
            atomic_write(self.path(task["id"], "task.json"), self.clean(json.dumps(task, indent=2, ensure_ascii=False)))
            self._announce(task, before, reason)

    def _remember(self, task: Dict[str, Any]) -> Dict[str, Any]:
        seen = {"status": task.get("status"), "questions": len(task.get("questions") or []),
                "notices": list(task.get("notices") or []), "rev": task.get("rev") or 0}
        if task.get("id"):
            self._seen[task["id"]] = seen
        return seen

    def _announce(self, task: Dict[str, Any], before: Optional[Dict[str, Any]], reason: str) -> None:
        """Events for what a save changed that readers act on: the status, a new question, a new notice."""
        after = self._remember(task)
        if before is None:
            return  # a new task: its "created" event says so
        if after["status"] != before["status"]:
            self.event(task["id"], "status", status=after["status"], previous=before["status"],
                       **({"reason": reason} if reason else {}))
        for q in (task.get("questions") or [])[before["questions"]:]:
            self.event(task["id"], "question_opened", question=q.get("id"), question_kind=q.get("kind"),
                       text=short(q.get("text") or "", 300))
        for n in after["notices"]:
            if n not in before["notices"]:
                self.event(task["id"], "notice", text=n)

    def load(self, task_id: str) -> Optional[Dict[str, Any]]:
        task = read_json(self.path(str(task_id), "task.json"))
        if task:
            with self._lock:
                self._remember(task)
        return task

    def unreadable(self) -> List[str]:
        """Ids of tasks whose record exists but can't be read (damaged by a power loss or by hand). Listed so they are
        noticed, never silently left out."""
        out = []
        for d in os.listdir(self.tasks_dir):
            if d.isdigit() and os.path.exists(self.path(d, "task.json")) and read_json(self.path(d, "task.json")) is None:
                out.append(d)
        return sorted(out, key=int)

    def all(self) -> List[Dict[str, Any]]:
        out = []
        for d in os.listdir(self.tasks_dir):
            t = self.load(d) if d.isdigit() else None
            if t:
                out.append(t)
        return sorted(out, key=lambda t: t["updated_at"], reverse=True)

    def event(self, task_id: str, kind: str, **data: Any) -> None:
        """Append one event: one line, one write. `at` is local time as in the records; `ts` (seconds since the epoch)
        is unambiguous across time zones and clock changes."""
        line = {"at": now_iso(), "ts": round(time.time(), 3), "event": kind, **data}
        payload = (self.clean(json.dumps(line, ensure_ascii=False)) + "\n").encode("utf-8")
        path = self.path(task_id, "events.jsonl")
        with self._lock:
            os.makedirs(self.path(task_id), exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
            try:
                if os.lseek(fd, 0, os.SEEK_END) and _last_byte(path) != b"\n":
                    payload = b"\n" + payload  # a line cut short by a crash: close it off first
                os.write(fd, payload)
            finally:
                os.close(fd)

    def events(self, task_id: str) -> List[Dict[str, Any]]:
        return self.read_events(task_id, 0, limit=0)[0]

    def read_events(self, task_id: str, cursor: int = 0, limit: int = 1000) -> Tuple[List[Dict[str, Any]], int, bool]:
        """Events after byte offset `cursor`: (events, next cursor, resync). Only complete lines are read; each event
        carries the cursor just after it. A line that doesn't parse is skipped. resync is True when the cursor doesn't
        fit this log (past its end, or not at the start of a line): the reader should reload the task record and go on
        from the returned cursor (the current end). limit 0: no limit."""
        path = self.path(str(task_id), "events.jsonl")
        try:
            with open(path, "rb") as f:
                size = f.seek(0, os.SEEK_END)
                if cursor < 0 or cursor > size or (cursor and _byte_at(f, cursor - 1) != b"\n"):
                    return [], _line_start(f, size), True
                f.seek(cursor)
                data = f.read()
        except OSError:
            return [], 0, cursor != 0
        out: List[Dict[str, Any]] = []
        pos = cursor
        for raw in data.split(b"\n")[:-1]:  # the last piece is a partial line (or empty): not read yet
            pos += len(raw) + 1
            try:
                ev = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            if isinstance(ev, dict):
                ev["cursor"] = pos
                out.append(ev)
                if limit and len(out) >= limit:
                    break
        return out, pos, False

    def write_log(self, task_id: str, name: str, text: str) -> str:
        p = self.path(task_id, "logs", re.sub(r"[^\w.-]", "_", name) + ".txt")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"===== {now_iso()}\n{self.clean(text)}\n")
        return p

    def write_report(self, task_id: str, text: str) -> None:
        atomic_write(self.path(task_id, "report.md"), self.clean(text))

    # ------------------------------------------------------------------ lookup
    def resolve(self, ref: Optional[str], cwd: Optional[str] = None, project_names: Optional[List[str]] = None,
                active_only: bool = False, fallback: bool = True) -> List[Dict[str, Any]]:
        """Candidate tasks for a reference: '#7'/'7', words from the title/request, or most recent in context.

        Returns best-first; a single element means unambiguous.
        """
        tasks = self.all()
        if active_only:
            tasks = [t for t in tasks if t["status"] in ACTIVE]
        if ref:
            m = re.fullmatch(r"#?(\d+)", ref.strip())
            if m:
                t = self.load(m.group(1))
                return [t] if t else []
            words = {w for w in re.findall(r"[a-z0-9]{3,}", ref.lower())} - _STOP
            if words:
                scored = []
                for t in tasks:
                    hay = " ".join([t["title"], t["request"], " ".join(t["projects"]), " ".join(t["remaining"]),
                                    " ".join(s.get("summary", "") for s in t["steps"])]).lower()
                    hits = sum(1 for w in words if w in hay)
                    if hits:
                        scored.append((hits / len(words), t))
                scored.sort(key=lambda x: x[1]["updated_at"], reverse=True)  # newest first within a score
                scored.sort(key=lambda x: -x[0])
                best = [t for s, t in scored if s >= 0.5 and s == scored[0][0]]
                if best:
                    return best  # more than one element means the reference is ambiguous
                if not fallback:
                    return []
        context = set(project_names or [])
        in_ctx = [t for t in tasks if context & set(t["projects"])] if context else []
        if cwd and not in_ctx:
            in_ctx = [t for t in tasks if t.get("cwd") == cwd]
        pool = in_ctx or tasks
        unfinished = [t for t in pool if t["status"] in ACTIVE]
        return (unfinished or pool)[:5]

    # ------------------------------------------------------------------ suggestions
    def _sugg_path(self) -> str:
        return os.path.join(self.home, "suggestions.json")

    def suggestions(self) -> List[Dict[str, Any]]:
        return read_json(self._sugg_path(), []) or []

    def add_suggestions(self, task_id: str, items: List[Dict[str, Any]], limit: int = 3) -> List[Dict[str, Any]]:
        """Record new major suggestions, skipping ones already open, accepted or dismissed. Returns the new ones."""
        added: List[Dict[str, Any]] = []

        def change(existing):
            existing = existing or []
            seen = {_norm(s["text"]) for s in existing}
            for it in items:
                if it.get("impact", "major") != "major":
                    continue  # minor items are the workers' job, not the user's
                text = (it.get("text") or "").strip()
                if not text or _norm(text) in seen or len(added) >= limit:
                    continue
                seen.add(_norm(text))
                sid = f"s{len(existing) + len(added) + 1}"
                added.append({"id": sid, "task": task_id, "project": it.get("project") or "",
                              "text": text, "why": it.get("why", ""), "status": "open", "created_at": now_iso()})
            return existing + added
        if any(it.get("impact", "major") == "major" and (it.get("text") or "").strip() for it in items):
            with self._lock:
                update_json(self._sugg_path(), change, [])
        return added

    def set_suggestion(self, sid: str, status: str) -> Optional[Dict[str, Any]]:
        found: List[Dict[str, Any]] = []

        def change(items):
            for s in items or []:
                if s["id"] == sid:
                    s["status"], s["decided_at"] = status, now_iso()
                    found.append(s)
            return items or []
        with self._lock:
            update_json(self._sugg_path(), change, [])
        return found[0] if found else None


def _byte_at(f, offset: int) -> bytes:
    f.seek(offset)
    return f.read(1)


def _last_byte(path: str) -> bytes:
    with open(path, "rb") as f:
        f.seek(-1, os.SEEK_END)
        return f.read(1)


def _line_start(f, size: int) -> int:
    """The offset just after the last complete line (where a reader that resyncs goes on from)."""
    f.seek(max(0, size - 65536))
    tail = f.read()
    i = tail.rfind(b"\n")
    return size - len(tail) + i + 1 if i >= 0 else (0 if size <= 65536 else size)


_STOP = {"the", "and", "for", "with", "that", "this", "task", "previous", "continue", "resume", "finish", "remaining",
         "work", "what", "state", "status", "current", "was", "working", "on", "my", "last", "of", "is", "are"}


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))[:120]
