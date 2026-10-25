"""Task store (~/.foreman/tasks/<id>/). Plain JSON + an append-only event log per task.

task.json holds the current state of a task: what was requested, which projects are involved, the plan
and step outcomes, decisions, questions, evidence (checks, diffs, reviews), what remains, and which
sessions contributed. events.jsonl is the audit trail; logs/ keeps raw worker output.
"""

import json
import os
import re
import threading
from typing import Any, Dict, List, Optional

from orchestrator.util import now_iso, read_json, short, write_json

ACTIVE = {"planning", "running", "needs_input", "blocked", "partial", "queued", "interrupted"}
FINAL = {"done", "failed", "answered", "cancelled"}


class Store:
    def __init__(self, home: str):
        self.home = home
        self.tasks_dir = os.path.join(home, "tasks")
        os.makedirs(self.tasks_dir, exist_ok=True)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ tasks
    def new_task(self, request: str, cwd: str) -> Dict[str, Any]:
        with self._lock:
            counter = os.path.join(self.tasks_dir, ".counter")
            n = (read_json(counter, 0) or 0) + 1
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
            self.event(task["id"], "created", request=request, cwd=cwd)
            return task

    def path(self, task_id: str, *parts: str) -> str:
        return os.path.join(self.tasks_dir, task_id, *parts)

    def save(self, task: Dict[str, Any]) -> None:
        with self._lock:
            task["updated_at"] = now_iso()
            write_json(self.path(task["id"], "task.json"), task)

    def load(self, task_id: str) -> Optional[Dict[str, Any]]:
        return read_json(self.path(str(task_id), "task.json"))

    def all(self) -> List[Dict[str, Any]]:
        out = []
        for d in os.listdir(self.tasks_dir):
            t = self.load(d) if d.isdigit() else None
            if t:
                out.append(t)
        return sorted(out, key=lambda t: t["updated_at"], reverse=True)

    def event(self, task_id: str, kind: str, **data: Any) -> None:
        with self._lock:
            os.makedirs(self.path(task_id), exist_ok=True)
            with open(self.path(task_id, "events.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"at": now_iso(), "event": kind, **data}, ensure_ascii=False) + "\n")

    def events(self, task_id: str) -> List[Dict[str, Any]]:
        try:
            with open(self.path(task_id, "events.jsonl"), encoding="utf-8") as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def write_log(self, task_id: str, name: str, text: str) -> str:
        p = self.path(task_id, "logs", re.sub(r"[^\w.-]", "_", name) + ".txt")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"===== {now_iso()}\n{text}\n")
        return p

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
        with self._lock:
            existing = self.suggestions()
            seen = {_norm(s["text"]) for s in existing}
            added = []
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
            if added:
                write_json(self._sugg_path(), existing + added)
            return added

    def set_suggestion(self, sid: str, status: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            items = self.suggestions()
            for s in items:
                if s["id"] == sid:
                    s["status"], s["decided_at"] = status, now_iso()
                    write_json(self._sugg_path(), items)
                    return s
        return None


_STOP = {"the", "and", "for", "with", "that", "this", "task", "previous", "continue", "resume", "finish", "remaining",
         "work", "what", "state", "status", "current", "was", "working", "on", "my", "last", "of", "is", "are"}


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))[:120]
