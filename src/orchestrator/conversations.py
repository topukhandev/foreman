"""Conversations: per-project transcripts shared by the terminal chat and the desktop app
(~/.foreman/chats/<project>/<id>.json). A conversation in one can be continued in the other.

Each entry is one message and Foreman's answer: {user, output, at, shown?, task?}. `task` is the id of the task the
message started or answered, so a front end can show that task where it happened. `turns` (the last few exchanges,
as text) and `focus` (the task the conversation is about) give the planner its context, the same in both front ends.
Storage only: what a message does is decided by the engine (route, prepare_answer).
"""

import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Union

from orchestrator.util import read_json, short, write_json

MAX_ENTRIES = 200
MAX_TURNS = 12


def folder_name(project: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", project).strip("-") or "default"


def task_line(task: Dict[str, Any]) -> str:
    """What a task came to, as a line of conversation context."""
    return (f"#{task['id']} {task['title']} — {task.get('outcome') or task['status']}"
            + (f": {short(task.get('summary') or '', 200)}" if task.get("summary") else ""))


class ChatHistory:
    """One project's conversations; `data` is the open one (a new one until a message is added or one is loaded)."""

    def __init__(self, home: str, project_key: Union[str, Callable[[], str]]):
        self.home = home
        self._key = project_key  # callable: resolved on first use, once the project index is loaded
        self.chat_id = ""
        self.start_new()

    @property
    def project(self) -> str:
        if callable(self._key):
            self._key = self._key()
        return self._key

    @property
    def dir(self) -> str:
        return os.path.join(self.home, "chats", folder_name(self.project))

    def start_new(self) -> None:
        self.chat_id = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()
        self.data: Dict[str, Any] = {"id": self.chat_id, "title": "", "entries": [],
                                     "turns": [], "focus": None, "started_at": time.time(), "updated_at": time.time()}

    def _write(self) -> None:
        write_json(os.path.join(self.dir, self.chat_id + ".json"), self.data)

    def append(self, user: str, output: str, turns, focus, shown: str = "", task: Optional[str] = None) -> None:
        self.data["project"] = self.project
        if user and not self.data["title"] and not user.startswith("/"):
            self.data["title"] = short(shown or user, 80)
        entry = {"user": user, "output": output, "at": time.time(), **({"shown": shown} if shown else {}),
                 **({"task": task} if task else {})}
        self.data["entries"] = (self.data["entries"] + [entry])[-MAX_ENTRIES:]
        self.data.update(turns=[list(t) for t in turns][-MAX_TURNS:], focus=focus, updated_at=time.time())
        self._write()

    def rename(self, name: str) -> None:
        """Name the conversation; saved now when it has messages, else with its first one."""
        self.data["title"], self.data["named"] = short(" ".join(name.split()), 80), True
        if self.data["entries"]:
            self._write()

    def load(self, chat_id: str) -> Optional[Dict[str, Any]]:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", chat_id or ""):
            return None
        data = read_json(os.path.join(self.dir, chat_id + ".json"))
        if data:
            self.chat_id, self.data = chat_id, data  # continue appending to the resumed conversation
        return data

    def list(self) -> List[Dict[str, Any]]:
        if not os.path.isdir(self.dir):
            return []
        items = [read_json(os.path.join(self.dir, f)) for f in os.listdir(self.dir) if f.endswith(".json")]
        return sorted([i for i in items if i], key=lambda i: (i.get("updated_at", 0), i.get("started_at", 0)), reverse=True)


def list_all(home: str) -> List[Dict[str, Any]]:
    """Every conversation with messages, newest first, without its entries: {id, project, title, started_at,
    updated_at, tasks, messages}."""
    root = os.path.join(home, "chats")
    out = []
    for folder in os.listdir(root) if os.path.isdir(root) else []:
        path = os.path.join(root, folder)
        if not os.path.isdir(path):
            continue
        for f in os.listdir(path):
            data = read_json(os.path.join(path, f)) if f.endswith(".json") else None
            if not data or not data.get("entries"):
                continue
            tasks = list(dict.fromkeys(e["task"] for e in data["entries"] if e.get("task")))
            out.append({"id": data.get("id") or f[:-5], "project": data.get("project") or folder,
                        "title": data.get("title") or "", "started_at": data.get("started_at", 0),
                        "updated_at": data.get("updated_at", 0), "tasks": tasks, "messages": len(data["entries"])})
    return sorted(out, key=lambda c: (c["updated_at"], c["started_at"]), reverse=True)


def context(turns: List[List[str]], entries: List[Dict[str, Any]], load_task: Callable[[str], Optional[Dict[str, Any]]],
            limit: int = 6) -> List[List[str]]:
    """The last exchanges for the planner, with each task's line brought up to date (a task started three messages
    ago may have finished since)."""
    fresh = []
    for e in entries[-limit:]:
        task = load_task(e["task"]) if e.get("task") else None
        fresh.append([e.get("user") or "", task_line(task) if task else short(e.get("output") or "", 300)])
    return fresh or [list(t) for t in turns[-limit:]]


def as_text(turns: List[List[str]]) -> str:
    return "\n".join(f"User: {short(u, 1200)}\nForeman: {a}" for u, a in turns)
