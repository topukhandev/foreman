"""OpenCode (`opencode run --format json`). Supports resume/fork and session discovery; read-only via the `plan` agent."""

import json
from typing import List

from orchestrator.providers.base import (GIT_POLICY, Capabilities, Provider, RunRequest, RunResult, fit_prompt,
                                         SessionInfo, tool_line)
from orchestrator.util import run_process


class OpenCodeProvider(Provider):
    name = "opencode"
    capabilities = Capabilities(resume=True, fork=True, structured_output=False,
                                session_discovery=True, enforced_read_only=True)

    def build_command(self, req: RunRequest) -> List[str]:
        req = fit_prompt(req)
        cmd = [self._binary or self.settings.command, "run", "--format", "json", "--dir", req.cwd]
        model = req.model or self.settings.model
        if model:
            cmd += ["-m", str(model)]
        if req.session_id:
            cmd += ["-s", req.session_id]
            if req.fork:
                cmd.append("--fork")
        if req.mode != "write":
            cmd += ["--agent", "plan"]
        prompt = req.prompt
        if req.extra_dirs:
            prompt += "\n\nRelated project directories you may read: " + ", ".join(req.extra_dirs)
        if req.mode == "write":
            prompt += "\n\n" + GIT_POLICY
        if req.schema:
            prompt += self.schema_instruction(req.schema)
        cmd += ["--", prompt]
        return cmd

    def run(self, req: RunRequest) -> RunResult:
        code, out, err, dur, timed_out = self._exec(self.build_command(req), req)
        texts, session_id, error = [], None, None
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            session_id = ev.get("sessionID") or session_id
            part = ev.get("part") or {}
            if ev.get("type") == "text" or part.get("type") == "text":
                texts.append(part.get("text") or ev.get("text") or "")
            if ev.get("type") == "error":
                data = (ev.get("error") or {}).get("data") or {}
                error = data.get("message") or json.dumps(ev.get("error"))[:400]
        text = "\n".join(t for t in texts if t).strip()
        if timed_out:
            error = f"timed out after {req.timeout}s"
        ok = code == 0 and not error and bool(text)
        data = self.parse_free_json(text) if req.schema else None
        if req.schema and data is None and ok:
            ok, error = False, "no structured JSON in reply"
        return RunResult(ok, text, session_id, data,
                         None if ok else (error or err.strip()[-800:] or f"exit {code}"), dur, raw=out)

    def activity(self, ev, cwd):
        part = ev.get("part") or {}
        if part.get("type") == "tool":
            state = part.get("state") or {}
            inp = state.get("input") or {}
            arg = inp.get("filePath") or inp.get("command") or inp.get("pattern") or inp.get("path") or ""
            return tool_line(str(part.get("tool", "tool")).capitalize(), arg, cwd)
        return None

    def discover_sessions(self, cwd: str) -> List[SessionInfo]:
        if not self._binary:
            return []
        res = run_process([self._binary, "session", "list", "--format", "json", "-n", "50"], cwd=cwd, timeout=20)
        try:
            items = json.loads(res.out) if res.code == 0 else []
        except ValueError:
            return []
        return [SessionInfo(self.name, s["id"], cwd, (s.get("updated") or 0) / 1000.0, s.get("title", ""))
                for s in items if s.get("directory") == cwd and s.get("id")]
