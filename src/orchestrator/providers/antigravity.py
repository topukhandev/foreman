"""Antigravity CLI (`agy`, verified against 1.4.7).

- JSON output (`--output-format json`) with `conversation_id`, `status`, `response`, `structured_output`.
- `--json-schema` for structured output; `--mode plan` for read-only steps; `--add-dir` for related projects.
- `--conversation=<id>` resumes; there is no fork, so the engine only resumes conversations it created itself.
- Headless mode cannot prompt, and any denied permission aborts the turn with no output, so both modes pass
  `--dangerously-skip-permissions`. Read steps add `--mode plan` + a read-only rule; Foreman flags any file the
  planner or a read step changes. Git safety: prompt policy plus the engine's HEAD check.
- `--model=<id>` takes the ids `agy models` lists. Some models come in effort variants that differ only by a suffix
  (gemini-3.8-flash-low/-medium/-high): Foreman treats those as one model with levels and builds the id. Models with
  a single id have no effort choice. (`--effort` also exists on agy but was not needed; verified 2026-10-26.)
- `-p` must be attached (`-p=<prompt>`), otherwise agy parses the next flag as the prompt.
- Web (verified 2026-10-25): plan mode has `search_web` (and `read_url_content`); agy cannot disable them per run,
  so the read-only rule tells it whether web research is allowed (prompt-level only).
- Usage (verified 2026-10-27 from a logged run): the result payload has usage.input_tokens / output_tokens /
  thinking_tokens / cache_read_tokens; no cost.
- Auth (checked 2026-10-27): the login lives in ~/.gemini; the binary also reads GEMINI_API_KEY, GOOGLE_API_KEY and
  GOOGLE_APPLICATION_CREDENTIALS, so those are the only secret-looking variables its runs keep (env_keep). agy has a
  `--sandbox` flag ("terminal restrictions"); not evaluated yet.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
import re
from typing import Dict, List

from orchestrator.providers.base import (GIT_POLICY, OUTPUT, Capabilities, ModelInfo, Provider, RunRequest, RunResult,
                                         SessionInfo, fit_prompt, sort_levels, stream_session_id, token_counts, tool_line)
from orchestrator.util import run_process


# Plan mode denies terminal commands; if the agent tries one anyway, agy aborts with no output.
READ_ONLY_RULE = ("Use only your built-in file reading and search tools. Terminal commands and MCP tools are "
                  "unavailable in this read-only session; do not attempt them.")
WEB_RULE = {True: " Web search and reading web pages are allowed for this research.",
            False: " Do not search the web; work from the code and documents."}


def antigravity_index() -> str:
    return os.path.expanduser(os.environ.get("ANTIGRAVITY_INDEX", "~/.gemini/antigravity/conversation_summaries.db"))


class AntigravityProvider(Provider):
    name = "antigravity"
    env_keep = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS")
    capabilities = Capabilities(resume=True, fork=False, structured_output=True,
                                session_discovery=True, enforced_read_only=True, web=True,
                                writes_extra_dirs=True, effort_in_model=True)

    def build_command(self, req: RunRequest) -> List[str]:
        req = fit_prompt(req)
        streamed = req.visual or req.on_activity is not None or req.on_session is not None
        cmd = [self._binary or self.settings.command, "--output-format", "stream-json" if streamed else "json"]
        model = req.model
        if model and req.effort:  # the effort is a variant of the model: gemini-3.8-flash + high = gemini-3.8-flash-high
            model = f"{model}-{req.effort}"
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
        if timed_out:  # verified: the first stream-json event is {"event":"init","conversation_id":...}
            return RunResult(False, error=f"timed out after {req.timeout}s", duration=dur, raw=out,
                             session_id=stream_session_id(out, "conversation_id"))
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
            except ValueError:  # died before its result (crash, network, killed): keep the conversation resumable
                return RunResult(False, out.strip()[-2000:], error=(err.strip() or f"exit {code}")[-800:], duration=dur,
                                 raw=out, session_id=stream_session_id(out, "conversation_id"))
        text = (payload.get("response") or "").strip()
        data = payload.get("structured_output") if req.schema else None
        if req.schema and data is None:
            data = self.parse_free_json(text)
        success = code == 0 and str(payload.get("status", "")).upper() == "SUCCESS"
        ok = success and (bool(text) or data is not None)
        error, kind = None, ""
        if req.schema and data is None:
            ok = False
            if success and text and not payload.get("denied_actions"):
                error, kind = "finished without the required structured result", OUTPUT
        if not ok and not error:
            denied = payload.get("denied_actions")
            error = (err.strip()[-600:] or text[-600:] or f"status {payload.get('status')}") + \
                (f"; denied: {denied}" if denied else "")
        sid = payload.get("conversation_id") or stream_session_id(out, "conversation_id")
        u = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        return RunResult(ok, text, sid, data, error, dur, raw=out, kind=kind,
                         tokens=token_counts(input=u.get("input_tokens"), output=u.get("output_tokens"),
                                             reasoning=u.get("thinking_tokens"), cache_read=u.get("cache_read_tokens")))

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

    def list_models(self) -> List[ModelInfo]:
        """From `agy models` (tab-separated id and name). Ids that differ only by a -low/-medium/-high suffix are one
        model with those effort levels; every other id is a model with a single fixed setting."""
        res = run_process([self._binary or self.settings.command, "models"], cwd=os.getcwd(), timeout=60)
        rows = [line.split("\t", 1) for line in res.out.splitlines() if "\t" in line]
        variants: Dict[str, Dict[str, str]] = {}
        for mid, name in rows:
            m = re.match(r"^(.*)-(minimal|low|medium|high|xhigh|max)$", mid.strip())
            if m:
                variants.setdefault(m.group(1), {})[m.group(2)] = re.sub(r"\s*\((?:minimal|low|medium|high|xhigh|max)\)\s*$",
                                                                       "", name.strip(), flags=re.I)
        out, seen = [], set()
        for mid, name in rows:
            mid = mid.strip()
            m = re.match(r"^(.*)-(minimal|low|medium|high|xhigh|max)$", mid)
            base = m.group(1) if m else None
            if base and len(variants.get(base, {})) > 1:
                if base not in seen:
                    seen.add(base)
                    out.append(ModelInfo(base, variants[base][m.group(2)], sort_levels(variants[base])))
            else:
                out.append(ModelInfo(mid, name.strip()))
        return out

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
