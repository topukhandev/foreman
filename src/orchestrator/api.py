"""The application API (docs/api-contract.md): the operations the desktop app and other clients use, as plain data.

Thin on purpose: every decision is the engine's (routing, locks, approvals, stop, reconciliation). This layer turns
requests into engine calls, starts task runners for anything that plans or runs (so no call waits for AI work), and
turns the engine's answers into data and error kinds a client can act on. The transport is separate (rpc_server.py).
"""

import hashlib
import os
import re
import sys
import time
from typing import Any, Callable, Dict, List, Optional

from orchestrator import __version__
from orchestrator.app import spawn_task
from orchestrator.config import ON_CLOSE, set_option
from orchestrator.engine import Engine
from orchestrator.modes import resolve as resolve_mode
from orchestrator.registry import REGISTRY_SCHEMA
from orchestrator.report import task_summary
from orchestrator.store import TASK_SCHEMA
from orchestrator.oscompat import FileLock
from orchestrator.util import read_text, update_json

API_VERSION = "1.1"  # 1.1: tasks.outputs / tasks.output (live output, read-only)
IDEMPOTENCY_TTL = 24 * 3600
# What a viewer must never receive from a CLI's raw output: terminal escape sequences (colours, cursor moves, window
# titles, hyperlinks, clipboard writes), other control characters, and bidirectional overrides that can make text
# read differently from what it is. Line breaks and tabs stay.
_ESCAPES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u202a-\u202e\u2066-\u2069]")


def plain_output(text: str) -> str:
    return _CONTROL.sub("", _ESCAPES.sub("", text))


class ApiError(Exception):
    """A request the API can't carry out. kind: not_found | ambiguous | running | stale | invalid | internal."""

    def __init__(self, kind: str, message: str, **data: Any):
        super().__init__(message)
        self.kind, self.data = kind, data


def _kind_of(message: str) -> Optional[str]:
    """The error kind of an engine refusal, or None when the message is information, not an error."""
    if " is running" in message:
        return "running"
    if message.startswith("stale"):
        return "stale"
    if message.startswith(("No task", "No suggestion", "No task found")):
        return "not_found"
    if message.startswith("Several tasks match"):
        return "ambiguous"
    return None


Spawner = Callable[[Dict[str, Any], bool], None]


class Api:
    def __init__(self, engine: Engine, actor: str = "desktop", spawn: Optional[Spawner] = None):
        self.eng = engine
        self.eng.actor = actor
        self.actor = actor
        # Starts a detached runner for a queued task (tests run it in-process instead).
        self._spawn = spawn or (lambda task, plan_only: spawn_task(self.eng.cfg.home, task["id"], None,
                                                                   run=task.get("run_request"), plan_only=plan_only))

    # ------------------------------------------------------------------ system
    def hello(self, client: str = "", api_version: str = API_VERSION) -> Dict[str, Any]:
        if str(api_version).split(".")[0] != API_VERSION.split(".")[0]:
            raise ApiError("invalid", f"this Foreman speaks API {API_VERSION}; the client asked for {api_version}")
        providers = []
        for name, prov in self.eng.providers.items():
            ok, info = prov.available()
            providers.append({"name": name, "available": ok, "info": info})
        return {"api_version": API_VERSION, "core_version": __version__, "home": self.eng.cfg.home,
                "platform": sys.platform, "actor": self.actor, "providers": providers,
                "schemas": {"task": TASK_SCHEMA, "registry": REGISTRY_SCHEMA}}

    def doctor(self) -> Dict[str, Any]:
        from orchestrator.cli import doctor_data
        return doctor_data(self.eng)

    # ------------------------------------------------------------------ tasks: reading
    def _task(self, task_id: str) -> Dict[str, Any]:
        task = self.eng.store.load(str(task_id))
        if not task:
            raise ApiError("not_found", f"No task #{task_id}.")
        return self.eng.reconcile(task)

    def summary(self, task: Dict[str, Any]) -> Dict[str, Any]:
        out = task_summary(task)
        out.update(rev=task.get("rev", 0), running=self.eng.is_running(task["id"]), created_at=task.get("created_at"),
                   questions=[{k: q.get(k) for k in ("id", "kind", "text", "header", "options", "descriptions",
                                                     "default", "plan_fp", "from_step", "project")}
                              for q in task.get("questions") or [] if not q.get("answer")])
        return out

    def tasks_list(self, status: Optional[str] = None, project: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
        tasks = self.eng.store.all()
        if status:
            tasks = [t for t in tasks if t["status"] == status]
        if project:
            tasks = [t for t in tasks if project in t.get("projects", {})]
        return {"tasks": [self.summary(t) for t in tasks[: max(1, int(limit))]], "unreadable": self.eng.store.unreadable()}

    def tasks_get(self, task_id: str) -> Dict[str, Any]:
        task = self._task(task_id)
        report = read_text(self.eng.store.path(task["id"], "report.md"))
        return {"summary": self.summary(task), "record": task, "report_md": report}

    def tasks_events(self, task_id: str, cursor: int = 0, limit: int = 1000) -> Dict[str, Any]:
        self._task(task_id)
        events, cursor, resync = self.eng.store.read_events(str(task_id), int(cursor), int(limit))
        return {"events": events, "cursor": cursor, "resync": resync}

    def tasks_outputs(self, task_id: str) -> Dict[str, Any]:
        """The live output logs of a task's provider runs (read-only)."""
        task = self._task(task_id)
        return {"outputs": self.eng.store.outputs(task["id"])}

    def tasks_output(self, task_id: str, name: str, offset: int = 0, limit: int = 65536) -> Dict[str, Any]:
        """Text of one live output log from a byte offset, already redacted when it was written and here stripped of
        terminal control sequences. `name` must be one of tasks.outputs (never a path)."""
        task = self._task(task_id)
        if name not in {o["name"] for o in self.eng.store.outputs(task["id"])}:
            raise ApiError("not_found", f"#{task['id']} has no output '{name}'.")
        text, offset, eof = self.eng.store.read_output(task["id"], name, max(0, int(offset)), min(int(limit), 1 << 20))
        return {"text": plain_output(text), "offset": offset, "eof": eof}

    # ------------------------------------------------------------------ tasks: changing
    def _start(self, task: Dict[str, Any], plan_only: bool = False) -> Dict[str, Any]:
        self._spawn(task, plan_only)
        return {"task_id": task["id"]}

    def _refused(self, message: str) -> None:
        kind = _kind_of(message)
        if kind:
            raise ApiError(kind, message)

    def tasks_submit(self, request: str, cwd: str, mode: Optional[str] = None, dry_run: bool = False,
                     confirm_plan: bool = False, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        if not str(request or "").strip():
            raise ApiError("invalid", "The request is empty.")
        if not os.path.isdir(cwd or ""):
            raise ApiError("invalid", f"No such folder: {cwd}")
        if mode and not resolve_mode(mode, self.eng.modes(cwd)):
            raise ApiError("invalid", f"Unknown mode '{mode}'. Modes: " + ", ".join(self.eng.modes(cwd)))

        def submit() -> Dict[str, Any]:
            task, msg = self.eng.route(request, cwd, confirm_plan=confirm_plan, mode=mode)
            if not task:
                self._refused(msg)
                return {"reply": msg}
            return self._start(task, plan_only=dry_run)
        return self._once(idempotency_key, "submit", submit)

    def tasks_answer(self, task_id: str, answers: Dict[str, str], plan_fp: Optional[str] = None,
                     idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        if not isinstance(answers, dict) or not answers:
            raise ApiError("invalid", "answers: give {question id: answer}.")

        def answer() -> Dict[str, Any]:
            task, msg = self.eng.prepare_answers(str(task_id), {str(k): str(v) for k, v in answers.items()}, plan_fp)
            if not task:
                kind = _kind_of(msg)
                if kind == "stale":
                    raise ApiError("stale", msg, task=self.summary(self._task(task_id)))
                self._refused(msg)
                return {"task_id": str(task_id), "queued": False, "message": msg}
            self._start(task)
            return {"task_id": task["id"], "queued": True, "message": ""}
        return self._once(idempotency_key, "answer", answer)

    def tasks_resume(self, task_id: str, note: str = "") -> Dict[str, Any]:
        task = self._task(task_id)
        queued, msg = self.eng.prepare_resume(task["id"], note, cwd=task["cwd"])
        if not queued:
            self._refused(msg)
            raise ApiError("invalid", msg)
        return self._start(queued)

    def tasks_next(self, cwd: str) -> Dict[str, Any]:
        task, msg = self.eng.prepare_next(cwd)
        if not task:
            return {"reply": msg}
        return self._start(task)

    def tasks_stop(self, task_id: str) -> Dict[str, Any]:
        task = self._task(task_id)
        return {"requested": self.eng.request_stop(task["id"])}

    def tasks_close(self, task_id: str, note: str = "", cancel: bool = False) -> Dict[str, Any]:
        self._task(task_id)
        res = self.eng.close(str(task_id), note, cancel=bool(cancel))
        self._refused(res["report"])
        task = res["task"] or self._task(task_id)
        return {"task": self.summary(task), "message": res["report"]}

    def _once(self, key: Optional[str], method: str, call: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
        """Run `call` once per idempotency key (within IDEMPOTENCY_TTL): a repeated request returns the first result,
        so a double click or a retried call doesn't start a second task or answer twice."""
        if not key:
            return call()
        path = os.path.join(self.eng.cfg.home, "idempotency.json")
        name = f"{method}:{key}"
        # one request per key at a time, across processes: two clicks at once still act once
        guard = FileLock(os.path.join(self.eng.cfg.home, "locks", "request-" + hashlib.sha256(name.encode()).hexdigest()[:16]))
        if not guard.acquire(30):
            raise ApiError("running", "The same request is still being handled.")
        try:
            seen = (update_json(path, lambda d: {k: v for k, v in (d or {}).items()
                                                 if time.time() - v.get("at", 0) < IDEMPOTENCY_TTL}, {}) or {}).get(name)
            if seen:
                return seen["result"]
            result = call()
            update_json(path, lambda d: {**(d or {}), name: {"at": time.time(), "result": result}}, {})
            return result
        finally:
            guard.release()

    # ------------------------------------------------------------------ projects, lifecycle, sessions
    def _project(self, p) -> Dict[str, Any]:
        reg = self.eng.registry
        rels = [{"type": r.type, "other": r.dst if r.src == p.name else r.src,
                 "direction": "out" if r.src == p.name else "in", "evidence": r.evidence, "source": r.source,
                 "confidence": r.confidence} for r in reg.relations_of(p.name)]
        return {"name": p.name, "path": p.path, "stack": list(p.stack), "test": p.test, "source": p.source,
                "relations": rels}

    def projects_list(self) -> Dict[str, Any]:
        self.eng.registry.ensure_scanned()
        return {"projects": [self._project(p) for p in sorted(self.eng.registry.projects.values(), key=lambda p: p.name)]}

    def projects_get(self, name: str) -> Dict[str, Any]:
        self.eng.registry.ensure_scanned()
        p = self.eng.registry.find(name)
        if not p:
            raise ApiError("not_found", f"No project '{name}'.")
        return self._project(p)

    def projects_scan(self) -> Dict[str, Any]:
        return {"projects": self.eng.registry.scan()}

    def lifecycle_get(self, cwd: str) -> Dict[str, Any]:
        return self.eng.lifecycle_data(cwd)

    def sessions_list(self, project: str) -> Dict[str, Any]:
        p = self.projects_get(project)
        out: List[Dict[str, Any]] = []
        for prov in self.eng.providers.values():
            out += [{"provider": prov.name, "session_id": s.session_id, "updated_at": s.updated_at, "title": s.title}
                    for s in prov.discover_sessions(p["path"])]
        return {"sessions": sorted(out, key=lambda s: -s["updated_at"])}

    # ------------------------------------------------------------------ suggestions and settings
    def suggestions_list(self, status: str = "open") -> Dict[str, Any]:
        return {"suggestions": [s for s in self.eng.store.suggestions() if not status or s["status"] == status]}

    def suggestions_accept(self, id: str, cwd: str) -> Dict[str, Any]:
        s = next((x for x in self.eng.store.suggestions() if x["id"] == id), None)
        if not s:
            raise ApiError("not_found", f"No suggestion {id}.")
        self.eng.store.set_suggestion(id, "accepted")
        task, msg = self.eng.accept_suggestion(s, cwd)
        return self._start(task) if task else {"reply": msg}

    def suggestions_dismiss(self, id: str) -> Dict[str, Any]:
        if not self.eng.store.set_suggestion(id, "dismissed"):
            raise ApiError("not_found", f"No suggestion {id}.")
        return {}

    def config_get(self) -> Dict[str, Any]:
        self.eng.reload_config()
        cfg = self.eng.cfg
        return {"display": cfg.display, "auto_switch": cfg.auto_switch, "notify": cfg.notify, "on_close": cfg.on_close,
                "planner": cfg.brain,
                "providers": {n: {"enabled": p.enabled, "model": p.chosen().model, "effort": p.chosen().effort}
                              for n, p in cfg.providers.items()}}

    def config_set_on_close(self, value: str) -> Dict[str, Any]:
        if value not in ON_CLOSE:
            raise ApiError("invalid", f"on_close is one of {', '.join(ON_CLOSE)}.")
        set_option(self.eng.cfg.home, "ui.on_close", value)
        self.eng.reload_config()
        return {}

