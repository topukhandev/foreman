"""The application API (docs/api-contract.md): the operations the desktop app and other clients use, as plain data.

Thin on purpose: every decision is the engine's (routing, locks, approvals, stop, reconciliation). This layer turns
requests into engine calls, starts task runners for anything that plans or runs (so no call waits for AI work), and
turns the engine's answers into data and error kinds a client can act on. The transport is separate (rpc_server.py).

Every front end goes through it: the desktop app (rpc_server.py), the MCP server and the CLI's --json output. The
methods under "Free-text front ends" are for the MCP server and the CLI only (they take task references in words and
free-text answers, and can wait for a task); they are not part of the JSON-RPC API.
"""

import hashlib
import os
import re
import sys
import time
from typing import Any, Callable, Dict, List, Optional

from orchestrator import __version__, conversations, settings
from orchestrator.app import spawn_task, wait_for
from orchestrator.config import ON_CLOSE
from orchestrator.conversations import ChatHistory
from orchestrator.engine import Engine, is_task_status_query
from orchestrator.modes import resolve as resolve_mode
from orchestrator.registry import REGISTRY_SCHEMA
from orchestrator.report import task_summary
from orchestrator.store import TASK_SCHEMA
from orchestrator.oscompat import FileLock
from orchestrator.util import read_text, short, update_json

API_VERSION = "1.5"  # 1.1: live output; 1.2: phase; 1.3: settings, models; 1.4: conversations; 1.5: opened projects
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
    def __init__(self, engine: Engine, actor: str = "desktop", spawn: Optional[Spawner] = None,
                 visual: Optional[bool] = None):
        """visual: how the runners this API starts show their workers (None: the user's setting)."""
        self.eng = engine
        self.eng.actor = actor
        self.actor = actor
        # Starts a detached runner for a queued task (tests run it in-process instead).
        self._spawn = spawn or (lambda task, plan_only: spawn_task(self.eng.cfg.home, task["id"], visual,
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

    def stats(self, since: Optional[str] = None) -> Dict[str, Any]:
        """Totals across tasks (1.5), as `foreman stats` shows them: {stats, text}."""
        from orchestrator.metrics import render_stats, stats
        data = stats(self.eng.store.all(), since)
        return {"stats": data, "text": render_stats(data)}

    # ------------------------------------------------------------------ tasks: reading
    def _task(self, task_id: str) -> Dict[str, Any]:
        task = self.eng.store.load(str(task_id))
        if not task:
            raise ApiError("not_found", f"No task #{task_id}.")
        return self.eng.reconcile(task)

    def summary(self, task: Dict[str, Any]) -> Dict[str, Any]:
        out = task_summary(task)
        home = self.eng.registry.project_for_path(task["cwd"]) if task.get("cwd") else None
        out.update(rev=task.get("rev", 0), running=self.eng.is_running(task["id"]), created_at=task.get("created_at"),
                   project=home.name if home else None, notices=list(task.get("notices") or [])[:5],
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
                "opened": p.opened, "relations": rels}

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

    def projects_open(self, path: str) -> Dict[str, Any]:
        """Open a folder the user picked (1.5): its project (registered now if Foreman didn't know it) is listed by
        every front end from now on. `opened` on a Project: epoch seconds, false when hidden, null when never."""
        path = os.path.abspath(os.path.expanduser(str(path or "")))
        if not os.path.isdir(path):
            raise ApiError("invalid", f"No such folder: {path}")
        if path in (os.path.abspath(os.sep), os.path.abspath(os.path.expanduser("~"))):
            raise ApiError("invalid", "Pick a project's folder, not the whole disk or your home folder.")
        self.eng.registry.ensure_scanned()
        return self._project(self.eng.registry.open(path, add=True))

    def projects_hide(self, name: str) -> Dict[str, Any]:
        """Take a project off the list until it is opened again (1.5). Its conversations and tasks stay."""
        p = self.eng.registry.hide(str(name))
        if not p:
            raise ApiError("not_found", f"No project '{name}'.")
        return self._project(p)

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

    def suggestions_accept(self, id: str, cwd: str, conversation_id: Optional[str] = None) -> Dict[str, Any]:
        """Start a task for a suggestion. With conversation_id (1.4), the task joins that conversation of the folder's
        project."""
        s = next((x for x in self.eng.store.suggestions() if x["id"] == id), None)
        if not s:
            raise ApiError("not_found", f"No suggestion {id}.")
        history = self._history(self._conversation_key(cwd), conversation_id) if conversation_id else None
        self.eng.store.set_suggestion(id, "accepted")
        task, msg = self.eng.accept_suggestion(s, cwd)
        if not task:
            return {"reply": msg}
        if history:
            line = f"#{task['id']} {task['title']} — started"
            said = f"Do this: {s['text']}"
            history.append(said, line, (history.data.get("turns") or []) + [[said, line]], task["id"], task=task["id"])
        return self._start(task)

    def suggestions_dismiss(self, id: str) -> Dict[str, Any]:
        if not self.eng.store.set_suggestion(id, "dismissed"):
            raise ApiError("not_found", f"No suggestion {id}.")
        return {}

    def config_get(self) -> Dict[str, Any]:
        """The settings the user can change (settings.py, the same list as `foreman config`) and each provider's model
        and effort."""
        self.eng.reload_config()
        cfg = self.eng.cfg
        providers = {}
        for n, p in cfg.providers.items():
            ok, info = self.eng.providers[n].available() if n in self.eng.providers else (False, "not set up")
            light = p.choice("light")
            providers[n] = {"enabled": p.enabled, "available": ok, "info": info, "model": p.chosen().model,
                            "effort": p.chosen().effort, "levels": list(p.effort_levels()),
                            "light": {"model": light.model, "effort": light.effort}}
        return {"display": cfg.display, "auto_switch": cfg.auto_switch, "notify": cfg.notify, "on_close": cfg.on_close,
                "planner": cfg.brain, "config_file": os.path.join(cfg.home, "config.yaml"),
                "settings": settings.describe(self.eng), "providers": providers}

    def config_set(self, key: str, value: Any) -> Dict[str, Any]:
        """Change one setting from config.get's list."""
        try:
            settings.change(self.eng, str(key), value)
        except ValueError as e:
            raise ApiError("invalid", str(e))
        return {"setting": next(x for x in settings.describe(self.eng) if x["key"] == key)}

    def config_set_on_close(self, value: str) -> Dict[str, Any]:
        if value not in ON_CLOSE:
            raise ApiError("invalid", f"on_close is one of {', '.join(ON_CLOSE)}.")
        self.config_set("on_close", value)
        return {}

    def _provider(self, provider: str):
        if provider not in self.eng.providers or not self.eng.cfg.providers[provider].enabled:
            raise ApiError("not_found", f"No provider '{provider}'.")
        return self.eng.providers[provider]

    def models_list(self, provider: str) -> Dict[str, Any]:
        """The models a provider's CLI offers now, with the effort levels each supports (asked of the CLI, so this can
        take a few seconds). `typed_levels`: the levels for a model name typed by hand."""
        prov = self._provider(provider)
        ok, info = prov.available()
        models = prov.list_models() if ok else []
        chosen = self.eng.cfg.providers[provider].chosen()
        return {"provider": provider, "available": ok, "info": info,
                "current": {"model": chosen.model, "effort": chosen.effort},
                "models": [{"id": m.id, "name": m.name, "levels": list(m.levels), "group": m.group} for m in models],
                "typed_levels": settings.levels_for_typed(models)}

    def models_set(self, provider: str, model: str, effort: Optional[str] = None) -> Dict[str, Any]:
        """Use this model ("default": the CLI decides) and effort for a provider from now on."""
        self._provider(provider)
        try:
            model, effort = settings.choose_model(self.eng, provider, model, effort)
        except ValueError as e:
            raise ApiError("invalid", str(e))
        return {"provider": provider, "model": model, "effort": effort}

    # ------------------------------------------------------------------ conversations (1.4)
    def _conversation_key(self, cwd: str) -> str:
        """The project a folder belongs to (its conversations are kept under that name), or the folder itself."""
        self.eng.registry.ensure_scanned(cwd)
        proj = self.eng.registry.project_for_path(cwd)
        return proj.name if proj else os.path.abspath(cwd)

    def _history(self, project: str, conversation_id: str) -> ChatHistory:
        history = ChatHistory(self.eng.cfg.home, str(project))
        if not history.load(str(conversation_id)):
            raise ApiError("not_found", f"No conversation {conversation_id} in {project}.")
        return history

    def conversations_list(self) -> Dict[str, Any]:
        """Every conversation with messages (terminal chat and app alike), newest first, without its messages."""
        return {"conversations": conversations.list_all(self.eng.cfg.home)}

    def conversations_get(self, project: str, conversation_id: str) -> Dict[str, Any]:
        """A conversation's messages, and the current state of every task they started."""
        data = self._history(project, conversation_id).data
        tasks = {}
        for e in data["entries"]:
            task = self.eng.store.load(e["task"]) if e.get("task") else None
            if task and task["id"] not in tasks:
                tasks[task["id"]] = self.summary(self.eng.reconcile(task))
        return {"conversation": {k: data.get(k) for k in ("id", "project", "title", "entries", "focus", "started_at",
                                                          "updated_at")}, "tasks": tasks}

    def conversations_send(self, cwd: str, text: str, conversation_id: Optional[str] = None, mode: Optional[str] = None,
                           dry_run: bool = False, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """A message in a conversation (a new one without conversation_id), handled as the terminal chat does: it
        answers the conversation's task when that task waits for answers, otherwise Foreman routes it with the
        conversation as context. Returns {conversation_id, project, task_id} when a task starts or continues, or
        {conversation_id, project, reply} for an instant answer."""
        if not str(text or "").strip():
            raise ApiError("invalid", "The message is empty.")
        if not os.path.isdir(cwd or ""):
            raise ApiError("invalid", f"No such folder: {cwd}")
        if mode and not resolve_mode(mode, self.eng.modes(cwd)):
            raise ApiError("invalid", f"Unknown mode '{mode}'. Modes: " + ", ".join(self.eng.modes(cwd)))
        project = self._conversation_key(cwd)

        def send() -> Dict[str, Any]:
            history = self._history(project, conversation_id) if conversation_id else ChatHistory(self.eng.cfg.home, project)
            data = history.data
            turns = conversations.context(data.get("turns") or [], data["entries"], self.eng.store.load)
            focus = self.eng.store.load(data["focus"]) if data.get("focus") else None
            answering = bool(focus and any(not q.get("answer") for q in focus["questions"])
                             and not is_task_status_query(text))
            if answering:
                task, msg = self.eng.prepare_answer(focus["id"], text)
            else:
                task, msg = self.eng.route(text, cwd, conversations.as_text(turns), confirm_plan=True, mode=mode)
            out = {"conversation_id": history.chat_id, "project": project}
            if task:
                self._start(task, plan_only=dry_run and not answering)
                line = f"#{task['id']} {task['title']} — " + ("answered, continuing" if answering else "started")
                history.append(text, line, turns + [[text, line]], task["id"], task=task["id"])
                return {**out, "task_id": task["id"]}
            history.append(text, msg, turns + [[text, short(msg, 300)]], data.get("focus"))
            return {**out, "reply": msg}
        return self._once(idempotency_key, "conversation", send)

    def conversations_rename(self, project: str, conversation_id: str, title: str) -> Dict[str, Any]:
        if not str(title or "").strip():
            raise ApiError("invalid", "Give the conversation a name.")
        history = self._history(project, conversation_id)
        history.rename(title)
        return {"title": history.data["title"]}

    # ------------------------------------------------------------------ free-text front ends (MCP, CLI; not JSON-RPC)
    def find(self, ref: str) -> Dict[str, Any]:
        """The task a reference names ('7', '#7' or words from its title): the best match."""
        cands = self.eng.store.resolve(str(ref), fallback=False)
        if not cands:
            raise ApiError("not_found", f"No task '{ref}'.")
        return self.eng.reconcile(cands[0])

    def status(self, ref: Optional[str], cwd: str) -> Dict[str, Any]:
        """A task's state from the records, as the engine words it (no AI): {task: TaskSummary | None, report}."""
        res = self.eng.status(ref, cwd)
        return {"task": self.summary(res["task"]) if res["task"] else None, "report": res["report"]}

    def resume_ref(self, ref: Optional[str], note: str = "", cwd: str = ".") -> Dict[str, Any]:
        """Continue a task named in words (or the latest unfinished one here): {task_id}."""
        task, msg = self.eng.prepare_resume(ref, note, cwd)
        if not task:
            self._refused(msg)
            raise ApiError("invalid", msg)
        return self._start(task)

    def answer_text(self, ref: Optional[str], text: str) -> Dict[str, Any]:
        """Answer a task's open questions in free text (one numbered line per question): {task_id} when it runs
        again, {reply} otherwise (e.g. the task was cancelled by a "no" to its approval)."""
        task, msg = self.eng.prepare_answer(ref or "", text)
        if not task:
            self._refused(msg)
            return {"reply": msg}
        return self._start(task)

    def projects_link(self, src: str, dst: str, type: str = "related", note: str = "") -> Dict[str, Any]:
        reg = self.eng.registry
        reg.ensure_scanned()
        a, b = reg.find(src), reg.find(dst)
        if not a or not b:
            raise ApiError("not_found", f"No project '{src if not a else dst}'.")
        r = reg.link(a.name, b.name, type or "related", note)
        return {"src": r.src, "dst": r.dst, "type": r.type}

    def wait(self, task_id: str, seconds: int) -> Dict[str, Any]:
        """Wait up to `seconds` for a started task to stop running: {summary, report_md, finished}."""
        task, finished = wait_for(self.eng, str(task_id), int(seconds))
        report = read_text(self.eng.store.path(task["id"], "report.md")) if finished else ""
        return {"summary": self.summary(task), "report_md": report, "finished": finished}
