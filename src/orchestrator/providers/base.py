"""Provider interface. Everything provider-specific lives behind this boundary."""

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from orchestrator.config import ProviderSettings
from orchestrator.finder import find_executable
from orchestrator.util import extract_json, run_process, run_streaming

# Opening lines of prompts Foreman sends; sessions starting with these are Foreman's, never the user's.
# "Switchyard" was the project's name for a while: sessions created under it are ours too.
OWN_PROMPT_PREFIXES = tuple(f"{opening} {name}" for name in ("Foreman", "Switchyard") for opening in (
    "You are the planning layer of", "You are a worker session coordinated by", "You are an independent reviewer for"))

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
    web: bool = False                # can search / fetch the web in read-only steps
    writes_extra_dirs: bool = False  # write steps can also write in extra_dirs (Foreman's progress notes)


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
    effort: Optional[str] = None           # reasoning effort (low|medium|high|xhigh|max) where the CLI has the option
    persist: bool = True                   # keep the session on disk (planning/review calls don't need to)
    visual: bool = False                   # show the worker in a visible terminal window
    title: str = ""                        # window title when visual
    on_activity: Optional[Callable[[str], None]] = field(default=None, repr=False)  # live "Read(x)" lines
    web: bool = False                      # read steps may search / fetch the web (R&D and planning modes)
    # Called once with the session id as soon as the CLI's output reveals it, so a run that is cut off (crash,
    # killed terminal, network) can still be resumed.
    on_session: Optional[Callable[[str], None]] = field(default=None, repr=False)


# A command-line argument can hold about 128 KB on Linux; a big paste in the request can exceed that.
MAX_PROMPT_BYTES = 90_000


def fit_prompt(req: RunRequest) -> RunRequest:
    """A request whose prompt fits on a command line. Longer prompts are written to a file the CLI is told to read
    first (its directory is added to the readable ones); the start and the end stay in the prompt itself."""
    if len(req.prompt.encode("utf-8")) <= MAX_PROMPT_BYTES:
        return req
    folder = os.path.join(tempfile.gettempdir(), "foreman-prompts")
    os.makedirs(folder, exist_ok=True)
    for name in os.listdir(folder):  # tidy up what earlier runs left behind
        old = os.path.join(folder, name)
        if time.time() - os.path.getmtime(old) > 86400:
            os.remove(old)
    path = os.path.join(folder, f"prompt-{os.getpid()}-{int(time.time() * 1000)}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(req.prompt)
    stub = (f"The full instructions are too long for the command line. They are in this file:\n{path}\n"
            f"Read it completely before doing anything and follow it exactly.\n\nStart of the instructions:\n"
            f"{req.prompt[:3000]}\n[...]\nEnd of the instructions:\n{req.prompt[-3000:]}")
    return replace(req, prompt=stub, extra_dirs=[*req.extra_dirs, folder])


def stream_session_id(out: str, key: str = "session_id") -> Optional[str]:
    """The session id in a CLI's JSON-lines output (Claude Code prints it in its first `system`/`init` line, agy as
    `conversation_id` in its `init` event), so a run that was cut off can still be resumed."""
    for line in out.splitlines()[:50]:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get(key):
            return str(ev[key])
    return None


# Why a run failed, in terms the engine has a policy for (see Engine._run_step).
TIMEOUT, LIMIT, TRANSIENT, AUTH, CONTEXT, OUTPUT, CRASH, STOPPED = (
    "timeout", "limit", "transient", "auth", "context", "output", "crash", "stopped")

LIMIT_RE = re.compile(r"usage.?limit|session limit|rate.?limit|quota|resource.?exhausted|too many requests|\b429\b|"
                      r"limit reached|out of (credits|requests)|credit balance|insufficient.?(credit|balance|quota)", re.I)
TRANSIENT_RE = re.compile(r"overloaded|\b(500|502|503|504|529)\b|internal server error|bad gateway|service unavailable|"
                          r"econnreset|econnrefused|etimedout|eai_again|enotfound|socket hang up|fetch failed|"
                          r"network (error|is unreachable)|connection (error|reset|refused|closed)|temporarily unavailable|"
                          r"unavailable_error|stream (closed|ended) unexpectedly|api error", re.I)
AUTH_RE = re.compile(r"api key|expired|unauthori[sz]ed|\b401\b|not logged in|log ?in required|authenticat|credential",
                     re.I)
CONTEXT_RE = re.compile(r"prompt is too long|context (length|window)|maximum context|too many tokens|"
                        r"input is too long|context_length_exceeded", re.I)


def classify(error: str) -> str:
    """Failure kind from an error text (adapters with structured errors may decide more precisely)."""
    text = error or ""
    if text.startswith("timed out"):
        return TIMEOUT
    if text.startswith("stopped by user"):
        return STOPPED
    if CONTEXT_RE.search(text):
        return CONTEXT
    if LIMIT_RE.search(text):
        return LIMIT
    if AUTH_RE.search(text):
        return AUTH
    if TRANSIENT_RE.search(text):
        return TRANSIENT
    return CRASH


def reset_time(text: str, now: Optional[float] = None) -> Optional[float]:
    """When a limit lifts, from messages such as "resets 7:10pm (Asia/Dhaka)", "resets at 19:10", "try again in
    15 minutes" or "retry after 120 seconds". None when the text doesn't say."""
    now = time.time() if now is None else now
    m = re.search(r"(?:try again|retry|resets?|available again)\s+(?:in|after)\s+(\d+)\s*(second|sec|s|minute|min|m|"
                  r"hour|hr|h)\w*\b", text or "", re.I)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        return now + n * (3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1)
    m = re.search(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:\(([\w/+-]+)\))?", text or "", re.I)
    if not m or (not m.group(2) and not m.group(3)):
        return None
    hour, minute, half, zone = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower(), m.group(4)
    if half == "pm" and hour < 12:
        hour += 12
    if half == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    tz = None
    if zone:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(zone)
        except Exception:
            tz = None
    current = datetime.fromtimestamp(now, tz) if tz else datetime.fromtimestamp(now)
    at = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if at.timestamp() <= now:
        at += timedelta(days=1)
    return at.timestamp()


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
    kind: str = ""                         # failure kind (TIMEOUT, LIMIT, ...); "" when ok
    retry_at: Optional[float] = None       # LIMIT: when the provider can be used again, if it said so
    usage: Optional[Dict[str, Any]] = None  # how close the provider is to its limit: {"utilization", "resets_at"}

    def __post_init__(self):
        if not self.ok and not self.kind:
            self.kind = classify(self.error or "")
        if self.kind == LIMIT and self.retry_at is None:
            self.retry_at = reset_time(self.error or "")


def tool_line(name: str, arg: Any, cwd: str) -> str:
    """Claude Code-style 'Tool(arg)' with paths shown relative to the project."""
    arg = " ".join(str(arg or "").split())
    if cwd and arg.startswith(cwd.rstrip("/") + "/"):
        arg = arg[len(cwd.rstrip("/")) + 1:]
    return f"{name}({arg[:400]})"  # the engine shortens it after turning project paths into names


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
        if not req.on_activity and not req.on_session:
            return None
        seen: List[str] = []

        def handle(line: str) -> None:
            try:
                ev = json.loads(line)
            except ValueError:
                return
            if not isinstance(ev, dict):
                return
            if req.on_session and not seen:
                sid = self.session_from_event(ev)
                if sid:
                    seen.append(sid)
                    req.on_session(sid)
            act = self.activity(ev, req.cwd) if req.on_activity else None
            if act:
                req.on_activity(act)
        return handle

    def activity(self, event: Dict[str, Any], cwd: str) -> Optional[str]:
        """One short human line ("Read(src/x.js)") for a streamed output event, or None."""
        return None

    def session_from_event(self, event: Dict[str, Any]) -> Optional[str]:
        """The session id carried by a streamed output event, if any."""
        sid = event.get("session_id") or event.get("sessionID") or event.get("conversation_id")
        return str(sid) if sid else None

    @staticmethod
    def schema_instruction(schema: Dict[str, Any]) -> str:
        return ("\n\nEnd your reply with a single fenced ```json block that matches this JSON schema exactly:\n"
                + json.dumps(schema))

    @staticmethod
    def parse_free_json(text: str) -> Optional[Dict[str, Any]]:
        return extract_json(text)
