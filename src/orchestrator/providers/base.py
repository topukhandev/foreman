"""Provider interface. Everything provider-specific lives behind this boundary."""

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from orchestrator.config import ProviderSettings
from orchestrator.finder import find_executable
from orchestrator.util import extract_json, run_process, run_streaming

# Opening lines of prompts Foreman sends; sessions starting with these are Foreman's, never the user's.
FOREMAN_PROMPT_PREFIXES = ("You are the planning layer of Foreman", "You are a worker session coordinated by Foreman",
                           "You are an independent reviewer for Foreman")

# Instruction appended to every worker prompt; enforced again by the engine's HEAD check.
GIT_POLICY = ("Never run git commit, git push, git reset, git checkout/switch of branches, git stash, or git clean. "
              "Leave all changes uncommitted in the working tree.")


@dataclass
class Capabilities:
    resume: bool = False             # continue a specific session by id
    fork: bool = False               # branch a session without mutating the original
    structured_output: bool = False  # native JSON-schema constrained output
    session_discovery: bool = False  # list existing sessions per directory
    enforced_read_only: bool = False  # tool-level (not just prompt-level) read-only mode


@dataclass
class RunRequest:
    prompt: str
    cwd: str
    mode: str = "read"                     # read | write
    session_id: Optional[str] = None       # resume this session
    fork: bool = False                     # fork instead of continuing in place
    extra_dirs: List[str] = field(default_factory=list)
    schema: Optional[Dict[str, Any]] = None
    timeout: int = 1200
    model: Optional[str] = None
    persist: bool = True                   # keep the session on disk (planning/review calls don't need to)
    visual: bool = False                   # show the worker in a visible terminal window
    title: str = ""                        # window title when visual
    on_activity: Optional[Callable[[str], None]] = field(default=None, repr=False)  # live "Read(x)" lines


@dataclass
class RunResult:
    ok: bool
    text: str = ""
    session_id: Optional[str] = None
    data: Optional[Dict[str, Any]] = None  # parsed structured output when a schema was requested
    error: Optional[str] = None
    duration: float = 0.0
    cost_usd: Optional[float] = None
    raw: str = ""


def tool_line(name: str, arg: Any, cwd: str) -> str:
    """Claude Code-style 'Tool(arg)' with paths shown relative to the project."""
    arg = " ".join(str(arg or "").split())
    if cwd and arg.startswith(cwd.rstrip("/") + "/"):
        arg = arg[len(cwd.rstrip("/")) + 1:]
    return f"{name}({arg[:90] + ('…' if len(arg) > 90 else '')})"


@dataclass
class SessionInfo:
    provider: str
    session_id: str
    cwd: str
    updated_at: float
    title: str = ""
    size_bytes: int = 0


class Provider:
    name = "base"
    capabilities = Capabilities()

    def __init__(self, settings: ProviderSettings):
        self.settings = settings
        self._binary = find_executable(settings.command) if settings.command else None

    def available(self) -> Tuple[bool, str]:
        if not self.settings.enabled:
            return False, "disabled in config"
        if not self._binary:
            return False, f"`{self.settings.command}` not found"
        return True, self._binary

    def run(self, req: RunRequest) -> RunResult:
        raise NotImplementedError

    def discover_sessions(self, cwd: str) -> List[SessionInfo]:
        return []

    # Shared helpers --------------------------------------------------------------

    def _exec(self, cmd: List[str], req: RunRequest) -> Tuple[int, str, str, float, bool]:
        on_line = self._line_handler(req)
        if req.visual:
            from orchestrator.terminal import run_in_terminal
            res = run_in_terminal(cmd, cwd=req.cwd, timeout=req.timeout, title=req.title or self.name, on_line=on_line)
            if res is not None:
                return res.code, res.out, res.err, res.duration, res.timed_out
        if on_line:
            res = run_streaming(cmd, cwd=req.cwd, timeout=req.timeout, on_line=on_line, env=dict(os.environ))
        else:
            res = run_process(cmd, cwd=req.cwd, timeout=req.timeout, env=dict(os.environ))
        return res.code, res.out, res.err, res.duration, res.timed_out

    def _line_handler(self, req: RunRequest):
        if not req.on_activity:
            return None

        def handle(line: str) -> None:
            try:
                ev = json.loads(line)
            except ValueError:
                return
            if isinstance(ev, dict):
                act = self.activity(ev, req.cwd)
                if act:
                    req.on_activity(act)
        return handle

    def activity(self, event: Dict[str, Any], cwd: str) -> Optional[str]:
        """One short human line ("Read(src/x.js)") for a streamed output event, or None."""
        return None

    @staticmethod
    def schema_instruction(schema: Dict[str, Any]) -> str:
        return ("\n\nEnd your reply with a single fenced ```json block that matches this JSON schema exactly:\n"
                + json.dumps(schema))

    @staticmethod
    def parse_free_json(text: str) -> Optional[Dict[str, Any]]:
        return extract_json(text)
