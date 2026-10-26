"""Claude Code (`claude -p`). Supports resume/fork, JSON-schema output, tool-level permissions, session discovery.
Web research (verified 2026-10-25): read steps with `web` add WebSearch/WebFetch to --allowedTools; without it
they are not allowed, so web access is enforced at the tool level."""

import json
import os
import re
from typing import Any, Dict, List, Optional

from orchestrator.providers.base import (GIT_POLICY, LIMIT, OUTPUT, TRANSIENT, Capabilities, Provider, RunRequest,
                                         RunResult, SessionInfo, fit_prompt, stream_session_id, tool_line)

READ_TOOLS = ["Read", "Grep", "Glob", "LS",
              "Bash(git log *)", "Bash(git diff *)", "Bash(git status *)", "Bash(git show *)",
              "Bash(ls *)", "Bash(cat *)", "Bash(find *)", "Bash(grep *)", "Bash(rg *)", "Bash(head *)",
              "Bash(tail *)", "Bash(wc *)", "Bash(command -v *)", "Bash(which *)", "Bash(git branch *)",
              "Bash(git rev-parse *)", "Bash(git remote *)", "Bash(pwd)"]
WRITE_DENY = ["Bash(git commit *)", "Bash(git push *)", "Bash(git reset *)", "Bash(git checkout *)",
              "Bash(git switch *)", "Bash(git stash *)", "Bash(git clean *)", "Bash(git rebase *)",
              "Bash(sudo *)", "Bash(rm -rf /*)", "Bash(rm -rf ~*)"]


def claude_projects_dir() -> str:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(base, "projects")


def encode_cwd(path: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", os.path.abspath(path))


class ClaudeProvider(Provider):
    name = "claude"
    capabilities = Capabilities(resume=True, fork=True, structured_output=True,
                                session_discovery=True, enforced_read_only=True, web=True,
                                writes_extra_dirs=True)

    def build_command(self, req: RunRequest) -> List[str]:
        req = fit_prompt(req)
        cmd = [self._binary or self.settings.command, "-p"]
        # stream-json gives live activity (and a readable visible terminal); its last line is the result object.
        streamed = req.visual or req.on_activity is not None or req.on_session is not None
        cmd += ["--output-format", "stream-json", "--verbose"] if streamed else ["--output-format", "json"]
        model = req.model or self.settings.model
        if model:
            cmd += ["--model", str(model)]
        if not req.persist and not req.session_id:
            cmd.append("--no-session-persistence")
        if req.session_id:
            cmd += ["--resume", req.session_id]
            if req.fork:
                cmd.append("--fork-session")
        for d in req.extra_dirs:
            cmd += ["--add-dir", d]
        if req.mode == "write":
            cmd += ["--permission-mode", "acceptEdits", "--allowedTools", "Bash", "Edit", "Write",
                    "--disallowedTools", *WRITE_DENY]
        else:
            cmd += ["--allowedTools", *READ_TOOLS, *(["WebSearch", "WebFetch"] if req.web else []),
                    "--disallowedTools", "Edit", "Write", "NotebookEdit"]
        if req.schema:
            cmd += ["--json-schema", json.dumps(req.schema)]
        # Prompt is passed after `--` so it can never be parsed as a flag (variadic tool lists above).
        prompt = req.prompt + ("\n\n" + GIT_POLICY if req.mode == "write" else "")
        cmd += ["--", prompt]
        return cmd

    def run(self, req: RunRequest) -> RunResult:
        code, out, err, dur, timed_out = self._exec(self.build_command(req), req)
        usage = _usage(out)
        if timed_out:  # verified: the first stream line carries session_id, and `--resume` keeps that id
            return RunResult(False, error=f"timed out after {req.timeout}s", session_id=stream_session_id(out),
                             duration=dur, raw=out, usage=usage)
        payload = {}
        for line in reversed(out.strip().splitlines()):
            try:
                cand = json.loads(line)
            except ValueError:
                continue
            if isinstance(cand, dict) and cand.get("type") == "result":
                payload = cand
                break
        if not payload:  # the process died before its result line (crash, network, killed)
            return RunResult(False, error=(err.strip() or out.strip() or f"exit {code}")[-800:], duration=dur, raw=out,
                             session_id=stream_session_id(out), usage=usage)
        text = payload.get("result") or ""
        ok = code == 0 and not payload.get("is_error")
        data = payload.get("structured_output") if req.schema else None
        if req.schema and data is None:
            data = self.parse_free_json(text)
        error, kind, retry_at = None, "", None
        if not ok:
            error = (text or err.strip() or f"exit {code}")[-800:]
            # Verified 2026-10-26: a usage limit ends with is_error, api_error_status 429, api_error
            # "usage_limit_reached", result "You've hit your session limit · resets 7:10pm (Asia/Dhaka)"; the
            # stream's rate_limit_event lines carry resetsAt (epoch seconds) and utilization.
            status = payload.get("api_error_status")
            if payload.get("api_error") == "usage_limit_reached" or status == 429:
                kind, retry_at = LIMIT, (usage or {}).get("resets_at")
            elif isinstance(status, int) and status >= 500:
                kind = TRANSIENT
        elif data is None and req.schema:
            ok, error, kind = False, "finished without the required structured result", OUTPUT
        return RunResult(
            ok=ok,
            text=text,
            session_id=payload.get("session_id"),
            data=data,
            error=error,
            duration=dur,
            cost_usd=payload.get("total_cost_usd"),
            raw=out,
            kind=kind,
            retry_at=retry_at,
            usage=usage,
        )

    def activity(self, ev, cwd):
        if ev.get("type") != "assistant":
            return None
        for part in (ev.get("message") or {}).get("content") or []:
            if part.get("type") == "tool_use":
                name, inp = part.get("name", "Tool"), part.get("input") or {}
                if name == "StructuredOutput":
                    return None
                arg = inp.get("file_path") or inp.get("command") or inp.get("pattern") or inp.get("path") \
                    or inp.get("url") or inp.get("description") or ""
                return tool_line(name, arg, cwd)
            if part.get("type") == "text" and part.get("text", "").strip():
                return "· " + " ".join(part["text"].split())[:140]
        return None

    def discover_sessions(self, cwd: str) -> List[SessionInfo]:
        folder = os.path.join(claude_projects_dir(), encode_cwd(cwd))
        if not os.path.isdir(folder):
            return []
        sessions = []
        for fname in os.listdir(folder):
            if not fname.endswith(".jsonl"):
                continue
            path = os.path.join(folder, fname)
            try:
                st = os.stat(path)
            except OSError:
                continue
            sessions.append(SessionInfo(self.name, fname[:-6], cwd, st.st_mtime, _first_prompt(path), st.st_size))
        return sorted(sessions, key=lambda s: s.updated_at, reverse=True)


def _usage(out: str) -> Optional[Dict[str, Any]]:
    """The last rate-limit reading in a stream-json run: {"utilization": 0.0-1.0, "resets_at": epoch seconds}."""
    for line in reversed(out.splitlines()):
        if '"rate_limit_info"' not in line:
            continue
        try:
            info = json.loads(line).get("rate_limit_info") or {}
        except (ValueError, AttributeError):
            continue
        if info.get("resetsAt"):
            return {"utilization": float(info.get("utilization") or (1.0 if info.get("status") == "rejected" else 0.0)),
                    "resets_at": float(info["resetsAt"]), "window": info.get("rateLimitType", "")}
    return None


def _first_prompt(path: str) -> str:
    """First real user prompt in a session transcript (skips command caveats/tool results)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for _, line in zip(range(200), f):
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") != "user" or ev.get("isMeta"):
                    continue
                content = (ev.get("message") or {}).get("content")
                if isinstance(content, list):
                    content = " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
                if isinstance(content, str) and content.strip() and not content.lstrip().startswith("<"):
                    return " ".join(content.split())[:120]
    except OSError:
        pass
    return ""
