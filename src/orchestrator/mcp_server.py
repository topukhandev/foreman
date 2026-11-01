"""MCP server (stdio, JSON-RPC 2.0). Lets any MCP-capable front end - Claude Code, Antigravity app/CLI, Codex,
desktop apps - hand work to the orchestrator. Long tasks run in a detached process; tools wait a bounded time
and otherwise return the task id for polling with `task_status`.
"""

import json
import os
import sys
from typing import Any, Dict, Optional

from orchestrator import __version__
from orchestrator.api import Api, ApiError, Spawner
from orchestrator.app import build_engine
from orchestrator.engine import Engine

INSTRUCTIONS = """\
Foreman coordinates AI coding sessions across the user's related projects. It knows which projects relate
(frontend/backend/tests/shared libs), keeps task history, picks and reuses sessions, runs validations itself,
and returns a short report.
- Use `orchestrate` for development work, especially work that may span projects, and for questions about
  previous tasks. Pass the user's request verbatim plus the current working directory.
- Use `task_status` for "what's the state of ..." (instant, no AI) and `resume_task` to continue previous work.
- If a report has "Needs your decision", ask the user (use your own question UI; offer the listed options, the one
  marked "recommended" first) and pass the reply to `answer_question`; for several questions send one answer per
  line, numbered ("1. ...", "2. ..."). Never answer questions on the user's behalf.
- A report that says "Stopped at …" or "Paused …" is unfinished work: `resume_task` continues it where it stopped,
  in the same sessions. A paused task (every provider out of its usage limit) needs no action until one is back.
- If a report has "Worth considering", show those items briefly as options; act only if the user accepts
  (`suggestion` with action=accept).
- Work moves through lifecycle modes: research (R&D), planning, development, qa, deployment. Foreman recommends
  one per request; pass `mode` only when the user asks for a specific one. Deployment mode prepares and verifies
  releases but never deploys.
- Use `project_state` for "where are we / what's next" (instant). If a report has "Next step", offer it to the
  user and call `next_step` if they accept.
- Show the report to the user as-is; it is already concise."""

_CWD = {"type": "string", "description": "Absolute path of the directory the user is working in."}
_WAIT = {"type": "integer", "description": "Seconds to wait for completion before returning (default 240)."}
_TASK = {"type": "string", "description": "Task number (e.g. '7') or words from its title. Omit for the most recent task here."}
_MODE = {"type": "string", "enum": ["research", "planning", "development", "qa", "deployment"],
         "description": "Only when the user asks for a specific mode; otherwise Foreman recommends one."}
_DRY = {"type": "boolean", "description": "Plan only: return what Foreman would do, run nothing. Use when the user asks "
                                          "for a preview or plan; `resume_task` then runs that plan."}
_DISPLAY = {"type": "string", "enum": ["terminal", "headless"],
            "description": "Show workers in visible terminal windows or run under the hood (default: user config)."}

TOOLS = [
    {"name": "orchestrate",
     "description": "Submit a request (task, change, investigation or question). Foreman selects the lifecycle mode, "
                    "projects, sessions and providers, executes, validates and reports.",
     "inputSchema": {"type": "object", "properties": {"request": {"type": "string"}, "cwd": _CWD, "wait_seconds": _WAIT,
                                                      "display": _DISPLAY, "mode": _MODE,
                                                      "dry_run": _DRY}, "required": ["request", "cwd"]}},
    {"name": "project_state", "description": "Lifecycle of the project: features, the stage each reached, the "
                                             "recommended next step, confirmed decisions (instant, no AI).",
     "inputSchema": {"type": "object", "properties": {"cwd": _CWD}, "required": ["cwd"]}},
    {"name": "next_step", "description": "Start the recommended next lifecycle step (e.g. QA after development) for the "
                                         "latest work in this project. Only when the user agrees.",
     "inputSchema": {"type": "object", "properties": {"cwd": _CWD, "wait_seconds": _WAIT, "display": _DISPLAY},
                     "required": ["cwd"]}},
    {"name": "task_status", "description": "Current state of a task from Foreman's records (instant, no AI).",
     "inputSchema": {"type": "object", "properties": {"task": _TASK, "cwd": _CWD}}},
    {"name": "resume_task", "description": "Continue an unfinished or previous task; re-verifies against the current code.",
     "inputSchema": {"type": "object", "properties": {"task": _TASK, "note": {"type": "string"}, "cwd": _CWD,
                                                      "wait_seconds": _WAIT, "display": _DISPLAY}}},
    {"name": "answer_question", "description": "Give the user's answer to a task's open question(s) and continue it. "
                                               "Several questions: one numbered answer per line (\"1. ...\").",
     "inputSchema": {"type": "object", "properties": {"task": _TASK, "answer": {"type": "string"}, "wait_seconds": _WAIT,
                                                      "display": _DISPLAY}, "required": ["answer"]}},
    {"name": "suggestion", "description": "Accept (starts a task for it) or dismiss (never shown again) a suggestion.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"},
                                                      "action": {"type": "string", "enum": ["accept", "dismiss"]},
                                                      "cwd": _CWD, "wait_seconds": _WAIT}, "required": ["id", "action"]}},
    {"name": "list_tasks", "description": "Recent tasks with status.",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
    {"name": "show_task", "description": "Full record of a task: steps, decisions, evidence, sessions.",
     "inputSchema": {"type": "object", "properties": {"task": _TASK}, "required": ["task"]}},
    {"name": "projects", "description": "Known projects and relations. action=list|scan|link. link needs src, dst, type, note.",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["list", "scan", "link"]}, "src": {"type": "string"},
         "dst": {"type": "string"}, "type": {"type": "string"}, "note": {"type": "string"}}}},
]


class MCPServer:
    def __init__(self, engine_factory=None, spawn: Optional[Spawner] = None):
        """spawn: how a queued task's runner starts (default: a detached process; tests run it in-process)."""
        self.engine_factory = engine_factory or (lambda visual=None: build_engine(visual=visual, out=lambda _m: None, actor="mcp"))
        self.spawn = spawn

    def _engine(self, display: Optional[str] = None) -> Engine:
        return self.engine_factory(None if not display else display == "terminal")

    # ------------------------------------------------------------------ tool handlers
    def call(self, name: str, args: Dict[str, Any]) -> str:
        """One tool call, as text for the model. Everything goes through the application API (api.py); a refusal the
        user should hear about (not found, several tasks match, still running) is the reply, not a tool error."""
        cwd = os.path.abspath(args.get("cwd") or os.getcwd())
        display = args.get("display")
        api = Api(self._engine(display), actor="mcp", spawn=self.spawn, visual=None if not display else display == "terminal")
        try:
            return self._call(api, name, args, cwd, int(args.get("wait_seconds", 240)))
        except ApiError as e:
            if e.kind in ("invalid", "internal"):
                raise
            return str(e)

    def _call(self, api: Api, name: str, args: Dict[str, Any], cwd: str, wait: int) -> str:
        if name == "orchestrate":
            return self._report(api, api.tasks_submit(args["request"], cwd, mode=args.get("mode"),
                                                      dry_run=bool(args.get("dry_run"))), wait)
        if name == "project_state":
            return api.eng.lifecycle_text(cwd)
        if name == "next_step":
            return self._report(api, api.tasks_next(cwd), wait)
        if name == "task_status":
            return api.status(args.get("task"), cwd)["report"]
        if name == "resume_task":
            return self._report(api, api.resume_ref(args.get("task"), args.get("note", ""), cwd), wait)
        if name == "answer_question":
            return self._report(api, api.answer_text(args.get("task"), args["answer"]), wait)
        if name == "suggestion":
            if args["action"] == "dismiss":
                api.suggestions_dismiss(args["id"])
                return f"Dismissed {args['id']}; it will not be suggested again."
            return self._report(api, api.suggestions_accept(args["id"], cwd), wait)
        if name == "list_tasks":
            tasks = api.tasks_list(limit=int(args.get("limit", 15)))["tasks"]
            return "\n".join(f"#{t['id']} [{t['status']}] {t['title']} ({t['updated_at']})" for t in tasks) or "No tasks."
        if name == "show_task":
            return json.dumps(api.tasks_get(api.find(args["task"])["id"])["record"], indent=1, ensure_ascii=False)[:60000]
        if name == "projects":
            action = args.get("action", "list")
            if action == "scan":
                return "Projects: " + ", ".join(api.projects_scan()["projects"])
            if action == "link":
                r = api.projects_link(args["src"], args["dst"], args.get("type", "related"), args.get("note", ""))
                return f"Linked {r['src']} --{r['type']}--> {r['dst']}"
            reg = api.eng.registry
            reg.ensure_scanned()
            return reg.render_context(reg.select("", cwd))
        raise ValueError(f"unknown tool {name}")

    @staticmethod
    def _report(api: Api, started: Dict[str, Any], wait: int) -> str:
        """An instant reply as it is; a started task: its report once it finishes within `wait` seconds, otherwise
        where it stands."""
        if "task_id" not in started:
            return started.get("reply") or started.get("message") or ""
        res = api.wait(started["task_id"], wait)
        task = res["summary"]
        if res["finished"]:
            return res["report_md"] or f"#{task['id']} {task['status']}"
        done = sum(1 for s in task["steps"] if s["status"] == "done")
        return (f"Task #{task['id']} is still running in the background ({done}/{len(task['steps'])} steps done). "
                f"Call task_status with task='{task['id']}' later; the user also gets a desktop notification when it finishes.")

    # ------------------------------------------------------------------ JSON-RPC
    def handle(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:
            return None  # notification
        if method == "initialize":
            return _ok(mid, {"protocolVersion": params.get("protocolVersion", "2024-11-05"),
                             "capabilities": {"tools": {}},
                             "serverInfo": {"name": "foreman", "version": __version__},
                             "instructions": INSTRUCTIONS})
        if method == "ping":
            return _ok(mid, {})
        if method == "tools/list":
            return _ok(mid, {"tools": TOOLS})
        if method == "tools/call":
            try:
                text = self.call(params.get("name", ""), params.get("arguments") or {})
                return _ok(mid, {"content": [{"type": "text", "text": text}]})
            except Exception as e:
                return _ok(mid, {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True})
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}

    def serve(self, stdin=sys.stdin, stdout=sys.stdout) -> None:
        for line in stdin:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}) + "\n")
                stdout.flush()
                continue
            resp = self.handle(msg)
            if resp is not None:
                stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                stdout.flush()


def _ok(mid, result) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}
