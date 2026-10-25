"""Antigravity CLI (`agy`, verified against 1.4.7).

- JSON output (`--output-format json`) with `conversation_id`, `status`, `response`, `structured_output`.
- `--json-schema` for structured output; `--mode plan` for read-only steps; `--add-dir` for related projects.
- `--conversation=<id>` resumes; there is no fork, so the engine only resumes conversations it created itself.
- Headless mode cannot prompt, and any denied permission aborts the turn with no output, so both modes pass
  `--dangerously-skip-permissions`. Read steps add `--mode plan` + a read-only rule; Foreman flags any file the
  planner or a read step changes. Git safety: prompt policy plus the engine's HEAD check.
- `-p` must be attached (`-p=<prompt>`), otherwise agy parses the next flag as the prompt.
- Web (verified 2026-10-25): plan mode has `search_web` (and `read_url_content`); agy cannot disable them per run,
  so the read-only rule tells it whether web research is allowed (prompt-level only).
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import List

from orchestrator.providers.base import (GIT_POLICY, Capabilities, Provider, RunRequest, RunResult,
                                         SessionInfo, tool_line)


# Plan mode denies terminal commands; if the agent tries one anyway, agy aborts with no output.
READ_ONLY_RULE = ("Use only your built-in file reading and search tools. Terminal commands and MCP tools are "
                  "unavailable in this read-only session; do not attempt them.")
WEB_RULE = {True: " Web search and reading web pages are allowed for this research.",
            False: " Do not search the web; work from the code and documents."}


def antigravity_index() -> str:
    return os.path.expanduser(os.environ.get("ANTIGRAVITY_INDEX", "~/.gemini/antigravity/conversation_summaries.db"))


class AntigravityProvider(Provider):
    name = "antigravity"
    capabilities = Capabilities(resume=True, fork=False, structured_output=True,
                                session_discovery=True, enforced_read_only=True, web=True)

    def build_command(self, req: RunRequest) -> List[str]:
        streamed = req.visual or req.on_activity is not None
        cmd = [self._binary or self.settings.command, "--output-format", "stream-json" if streamed else "json"]
        model = req.model or self.settings.model
        if model:
            cmd.append(f"--model={model}")
        if req.session_id:
            cmd.append(f"--conversation={req.session_id}")
        for d in req.extra_dirs:
            cmd.append(f"--add-dir={d}")
        if req.mode == "write":
            cmd += ["--mode=accept-edits", "--dangerously-skip-permissions"]
        else:
            # Headless agy aborts the whole turn when any permission is denied (even a harmless read outside the
            # project), so read steps run in plan mode without prompts; the read-only rule plus Foreman's
            # file-change checks keep them honest.
            cmd += ["--mode=plan", "--dangerously-skip-permissions"]
        if req.schema:
            cmd.append("--json-schema=" + json.dumps(req.schema))
        prompt = req.prompt + "\n\n" + (GIT_POLICY if req.mode == "write" else READ_ONLY_RULE + WEB_RULE[req.web])
        cmd.append(f"-p={prompt}")
        return cmd

    def run(self, req: RunRequest) -> RunResult:
        code, out, err, dur, timed_out = self._exec(self.build_command(req), req)
        if timed_out:
            return RunResult(False, error=f"timed out after {req.timeout}s", duration=dur, raw=out)
        payload = {}
        for line in reversed(out.strip().splitlines()):
            try:
                cand = json.loads(line)
            except ValueError:
                continue
            if isinstance(cand, dict) and cand.get("event") == "result" and isinstance(cand.get("result"), dict):
                payload = cand["result"]  # stream-json wraps the final payload
                break
            if isinstance(cand, dict) and ("response" in cand or "status" in cand) and "event" not in cand:
                payload = cand
                break
        if not payload:
            try:
                payload = json.loads(out)
            except ValueError:
                return RunResult(False, out.strip(), error=(err.strip() or f"exit {code}")[-800:], duration=dur, raw=out)
        text = (payload.get("response") or "").strip()
        data = payload.get("structured_output") if req.schema else None
        if req.schema and data is None:
            data = self.parse_free_json(text)
        ok = code == 0 and str(payload.get("status", "")).upper() == "SUCCESS" and (bool(text) or data is not None)
        if req.schema and data is None:
            ok = False
        error = None
        if not ok:
            denied = payload.get("denied_actions")
            error = (err.strip()[-600:] or f"status {payload.get('status')}") + (f"; denied: {denied}" if denied else "")
        return RunResult(ok, text, payload.get("conversation_id"), data, error, dur, raw=out)

    def activity(self, ev, cwd):
        su = ev.get("step_update") or {}
        if su.get("step_type") == "tool" and su.get("state") == "ACTIVE":
            info = su.get("tool_info") or {}
            params = info.get("parameters") or {}
            arg = next((v for k, v in params.items() if k.lower() in ("path", "command", "commandline", "query",
                                                                       "pattern", "file", "absolutepath", "url")), "")
            name = {"read_file": "Read", "run_command": "Bash", "grep_search": "Grep", "list_dir": "List",
                    "write_to_file": "Write", "replace_file_content": "Edit", "find_by_name": "Glob",
                    "view_file": "Read", "search_web": "WebSearch", "read_url_content": "WebFetch"}.get(info.get("name") or su.get("tool_name"), info.get("name") or "Tool")
            return tool_line(name, arg, cwd)
        return None

    def discover_sessions(self, cwd: str) -> List[SessionInfo]:
        db = antigravity_index()
        if not os.path.exists(db):
            return []
        uri = "file://" + os.path.abspath(cwd)
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
            rows = con.execute("SELECT conversation_id, title, preview, last_modified_time, workspace_uris "
                               "FROM conversation_summaries").fetchall()
            con.close()
        except sqlite3.Error:
            return []
        out = []
        for cid, title, preview, modified, uris in rows:
            try:
                if uri not in json.loads(uris or "[]"):
                    continue
            except ValueError:
                continue
            out.append(SessionInfo(self.name, cid, cwd, _ts(modified), title or preview or ""))
        return sorted(out, key=lambda s: s.updated_at, reverse=True)


def _ts(value: str) -> float:
    try:
        base = str(value).split("+")[0][:26].replace(" ", "T")
        return datetime.fromisoformat(base).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0
