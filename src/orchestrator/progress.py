"""What a running task is doing right now, in a few words, from its events: the chat's status line and the desktop
app's progress line (the API feed's `phase` notification) both come from here. Presentation only; no decisions."""

import re
from typing import Any, Dict, Tuple

from orchestrator.report import clock
from orchestrator.util import short

TOOL_VERBS = {"read": "Reading", "view": "Reading", "grep": "Searching for", "search": "Searching for",
              "glob": "Looking for files", "list": "Browsing", "ls": "Browsing", "edit": "Editing",
              "multiedit": "Editing", "patch": "Editing", "write": "Writing", "bash": "Running",
              "webfetch": "Reading", "websearch": "Searching the web for", "task": "Delegating",
              "agent": "Delegating", "todowrite": "Organising the work"}
QUESTION_RE = re.compile(r"\?\s*$|^\s*(what|how|where|why|which|who|when|is|are|does|do|can|could|should|explain|"
                         r"show me|tell me|list|find|check|review|compare)\b", re.I)


def intent_verb(task: Dict[str, Any]) -> str:
    """First spinner verb, from the request (before the planner has said anything)."""
    if any(s.get("status") in ("done", "failed", "blocked") for s in task.get("steps") or []):
        return "Picking up where it left off"
    return "Looking into it" if QUESTION_RE.search(task.get("request") or "") else "Planning the change"


class Phase:
    """Turns task events into the spinner's '<what is happening>' line and the active provider.
    Tool activity wins (Reading server.js, Running npm test); otherwise the phase (implementing, checks, review)."""

    def __init__(self, task: Dict[str, Any]):
        self.verb, self.who = intent_verb(task), ""
        self.running: Dict[str, Tuple[str, str]] = {}  # project -> (phase verb, provider)

    def update(self, ev: Dict[str, Any]) -> bool:
        """Apply one event; True when the line changed."""
        before = (self.verb, self.who)
        e, provider = ev.get("event"), ev.get("provider")
        if e == "thinking":
            purpose = ev.get("purpose")
            self.verb = {"replan": "Expanding the plan", "review": "Reviewing the changes",
                         "document": "Writing the document"}.get(purpose, self.verb)
            self.who = provider or self.who
        elif e == "impact":
            self.verb = "Checking the impact before editing"
        elif e == "plan":
            steps = (ev.get("plan") or {}).get("steps") or []
            projects = sorted({s.get("project") for s in steps if s.get("project")})
            self.verb = "Writing the answer" if not steps else \
                f"Starting work on {projects[0]}" if len(projects) == 1 else f"Starting work on {len(projects)} projects"
        elif e == "step_start":
            project = ev.get("project", "")
            verb = {"investigate": f"Investigating {project}", "verify": f"Verifying the changes (QA) in {project}"}.get(
                ev.get("step_kind"), f"Implementing in {project}")
            self.running[project] = (verb, provider or "")
            self.verb, self.who = verb, provider or self.who
        elif e == "step_failed":  # another provider may retry the step; the next step_start says so
            self.verb = "Recovering from a failed attempt"
        elif e == "waiting":
            self.verb, self.who = f"Waiting for a provider (next try at {clock(ev.get('until'))})", ""
        elif e == "step_done":
            self.running.pop(ev.get("project", ""), None)
            if self.running:
                self.verb, self.who = next(iter(self.running.values()))
            else:
                self.verb = "Checking the results"
        elif e == "check":
            self.verb, self.who = "Running checks", ""
        elif e == "review":
            self.verb = "Writing the report"
        elif e == "activity":
            self.verb = self._activity(ev)
            self.who = provider or self.who
        elif e == "finished":
            self.verb = "Finishing"
        return (self.verb, self.who) != before

    def _activity(self, ev: Dict[str, Any]) -> str:
        text, where = ev.get("text", ""), ev.get("where") or ""
        in_project = where in self.running and len(self.running) > 1
        m = re.match(r"(\w+)\((.*)\)$", text)
        if not m:  # assistant text: the model is reasoning between tool calls
            return f"Thinking about {where}" if in_project else "Thinking"
        name, arg = m.group(1), m.group(2)
        action = TOOL_VERBS.get(name.lower(), f"Using {name}")
        if action.startswith(("Reading", "Editing", "Writing")) and "/" in arg and not arg.startswith("http"):
            arg = arg.rstrip("/").rsplit("/", 1)[-1]
        elif action == "Running":
            arg = arg.split("&&")[0].split("|")[0].strip()
        if action in ("Looking for files", "Browsing", "Organising the work", "Delegating"):
            arg = ""
        line = f"{action} {short(arg, 40)}".strip() if arg else action
        return line + (f" in {where}" if in_project else "")
