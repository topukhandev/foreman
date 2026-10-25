"""MCP server (stdio, JSON-RPC 2.0). Lets any MCP-capable front end - Claude Code, Antigravity app/CLI, Codex,
desktop apps - hand work to the orchestrator. Long tasks run in a detached process; tools wait a bounded time
and otherwise return the task id for polling with `task_status`.
"""

import json
import os
import sys
from typing import Any, Dict, Optional

from orchestrator import __version__
from orchestrator.app import build_engine, read_report, spawn_task, wait_for
from orchestrator.engine import Engine

INSTRUCTIONS = """\
Foreman coordinates AI coding sessions across the user's related projects. It knows which projects relate
(frontend/backend/tests/shared libs), keeps task history, picks and reuses sessions, runs validations itself,
and returns a short report.
- Use `orchestrate` for development work, especially work that may span projects, and for questions about
  previous tasks. Pass the user's request verbatim plus the current working directory.
- Use `task_status` for "what's the state of ..." (instant, no AI) and `resume_task` to continue previous work.
- If a report has "Needs your decision", ask the user (use your own question UI) and pass the reply to
  `answer_question`. Never answer business questions on the user's behalf.
- If a report has "Worth considering", show those items briefly as options; act only if the user accepts
  (`suggestion` with action=accept).
- Show the report to the user as-is; it is already concise."""

_CWD = {"type": "string", "description": "Absolute path of the directory the user is working in."}
_WAIT = {"type": "integer", "description": "Seconds to wait for completion before returning (default 240)."}
_TASK = {"type": "string", "description": "Task number (e.g. '7') or words from its title. Omit for the most recent task here."}
_DISPLAY = {"type": "string", "enum": ["terminal", "headless"],
            "description": "Show workers in visible terminal windows or run under the hood (default: user config)."}

TOOLS = [
    {"name": "orchestrate",
     "description": "Submit a request (task, change, investigation or question). Foreman selects projects, sessions "
                    "and providers, executes, validates and reports.",
     "inputSchema": {"type": "object", "properties": {"request": {"type": "string"}, "cwd": _CWD, "wait_seconds": _WAIT,
                                                      "display": _DISPLAY}, "required": ["request", "cwd"]}},
    {"name": "task_status", "description": "Current state of a task from Foreman's records (instant, no AI).",
     "inputSchema": {"type": "object", "properties": {"task": _TASK, "cwd": _CWD}}},
    {"name": "resume_task", "description": "Continue an unfinished or previous task; re-verifies against the current code.",
     "inputSchema": {"type": "object", "properties": {"task": _TASK, "note": {"type": "string"}, "cwd": _CWD,
                                                      "wait_seconds": _WAIT, "display": _DISPLAY}}},
    {"name": "answer_question", "description": "Give the user's answer to a task's open question and continue it.",
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
    def __init__(self, engine_factory=None):
        self.engine_factory = engine_factory or (lambda visual=None: build_engine(visual=visual, out=lambda _m: None))

    def _engine(self, display: Optional[str] = None) -> Engine:
        return self.engine_factory(None if not display else display == "terminal")

    # ------------------------------------------------------------------ tool handlers
    def call(self, name: str, args: Dict[str, Any]) -> str:
        cwd = os.path.abspath(args.get("cwd") or os.getcwd())
        wait = int(args.get("wait_seconds", 240))
        display = args.get("display")
        eng = self._engine(display)
        visual = None if not display else display == "terminal"

        if name == "orchestrate":
            task, msg = eng.route(args["request"], cwd)
            return self._launch(eng, task, msg, wait, visual)
        if name == "task_status":
            return eng.status(args.get("task"), cwd)["report"]
        if name == "resume_task":
            task, msg = eng.prepare_resume(args.get("task"), args.get("note", ""), cwd)
            return self._launch(eng, task, msg, wait, visual)
        if name == "answer_question":
            task, msg = eng.prepare_answer(args.get("task") or "", args["answer"])
            return self._launch(eng, task, msg, wait, visual)
        if name == "suggestion":
            s = eng.store.set_suggestion(args["id"], "accepted" if args["action"] == "accept" else "dismissed")
            if not s:
                return f"No suggestion {args['id']}."
            if args["action"] == "dismiss":
                return f"Dismissed {s['id']}; it will not be suggested again."
            proj = eng.registry.projects.get(s["project"])
            return self._launch(eng, eng.prepare_new(s["text"], proj.path if proj else cwd), "", wait, visual)
        if name == "list_tasks":
            tasks = eng.store.all()[: int(args.get("limit", 15))]
            return "\n".join(f"#{t['id']} [{t['status']}] {t['title']} ({t['updated_at']})" for t in tasks) or "No tasks."
        if name == "show_task":
            cands = eng.store.resolve(args["task"], fallback=False)
            return json.dumps(cands[0], indent=1, ensure_ascii=False)[:60000] if cands else "Not found."
        if name == "projects":
            action = args.get("action", "list")
            if action == "scan":
                return "Projects: " + ", ".join(eng.registry.scan())
            if action == "link":
                r = eng.registry.link(args["src"], args["dst"], args.get("type", "related"), args.get("note", ""))
                return f"Linked {r.src} --{r.type}--> {r.dst}"
            eng.registry.ensure_scanned()
            sel = eng.registry.select("", cwd)
            return eng.registry.render_context(sel)
        raise ValueError(f"unknown tool {name}")

    def _launch(self, eng: Engine, task, msg: str, wait: int, visual) -> str:
        if not task:
            return msg
        spawn_task(eng.cfg.home, task["id"], visual)
        task, finished = wait_for(eng, task["id"], wait)
        if finished:
            return read_report(eng, task["id"]) or f"#{task['id']} {task['status']}"
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
