"""OpenCode (`opencode run --format json`). Supports resume/fork and session discovery; read-only via the `plan` agent.

OpenCode 2.x (verified 2026-11-01 against v2.1.4: `run --help`, `models --help`, `api provider.list`, live runs):
- `--dir` is gone. By default runs go to a shared background service whose directory is the user's home, not the
  caller's (`api provider.list` reports location /home/<user> from any folder), so a worker would read and edit
  the wrong folder. `--standalone` starts a private server in the process's working directory (verified: it
  reports the project folder), so every run uses it.
- `run` also reads the message from stdin when stdin is not a terminal: runs need stdin closed (run_process and
  run_streaming already pass DEVNULL), or they wait for it.
- Effort: `--variant` is gone; a model variant is `-m provider/model#variant` (from `run --help`; not run live).
- Write steps add `--auto` ("auto-approve permissions that are not explicitly denied", from `run --help`), so a
  worker can't stall at a permission prompt nobody can answer. Not verified live: no model was usable on the
  machine (OpenRouter without credits, an expired opencode-go key).
- Errors: `{"type": "error", "error": {"type": "provider.auth", "message": ...}}` (verified live), classified
  by providers.base.classify (credits and expired keys rest the provider like a login problem).
- `models --verbose` is gone: `models` lists `provider/model` ids only, so effort levels are unknown (verified).
OpenCode 1.x (1.18, verified 2026-10-26): `--dir <cwd>`, `--variant <level>`, `models --verbose` with each model's
variants. The major version is read from `opencode --version` and the matching flags are used.
Usage (verified 2026-10-27 from a logged run): every `step_finish` event's part has tokens {input, output, reasoning,
cache: {read, write}} and cost; a run's usage is their sum.
Auth: `opencode auth login` keeps keys in ~/.local/share/opencode/auth.json, so its runs get no secret-looking
environment variables; a backend configured through an environment key needs providers.opencode.env_keep."""

import json
import os
import re
from typing import List, Optional

from orchestrator.providers.base import (GIT_POLICY, OUTPUT, Capabilities, ModelInfo, Provider, RunRequest, RunResult,
                                         SessionInfo, fit_prompt, sort_levels, token_counts, tool_line)
from orchestrator.util import run_process


class OpenCodeProvider(Provider):
    name = "opencode"
    capabilities = Capabilities(resume=True, fork=True, structured_output=False,
                                session_discovery=True, enforced_read_only=True)

    _major: Optional[int] = None

    def major_version(self) -> int:
        """The installed OpenCode's major version (2 when it can't be read: the current one)."""
        if self._major is None:
            self._major = 2
            if self._binary:
                res = run_process([self._binary, "--version"], cwd=os.getcwd(), timeout=15)
                m = re.search(r"(\d+)\.\d+", res.out or "")
                if res.code == 0 and m:
                    self._major = int(m.group(1))
        return self._major

    def build_command(self, req: RunRequest) -> List[str]:
        req = fit_prompt(req, self.batch_shim)
        v2 = self.major_version() >= 2
        cmd = [self._binary or self.settings.command, "run", "--format", "json"]
        # 2.x: a private server in the project's folder (the background service works in the home folder instead)
        cmd += ["--standalone"] if v2 else ["--dir", req.cwd]
        if req.model:
            model = str(req.model)
            if req.effort and v2:
                model += f"#{req.effort}"  # 2.x: the effort is a variant of the model
            cmd += ["-m", model]
        if req.effort and not v2:  # 1.x: `--variant`, the model's provider-specific reasoning effort
            cmd += ["--variant", req.effort]
        if req.mode == "write" and v2:
            cmd.append("--auto")  # nobody can answer a permission prompt in a background run
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
        used = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}
        cost = 0.0
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            session_id = ev.get("sessionID") or session_id
            part = ev.get("part") or {}
            if ev.get("type") == "text" or part.get("type") == "text":
                texts.append(part.get("text") or ev.get("text") or "")
            if ev.get("type") == "step_finish" or part.get("type") == "step-finish":
                t = part.get("tokens") or {}
                cache = t.get("cache") or {}
                for k, v in (("input", t.get("input")), ("output", t.get("output")), ("reasoning", t.get("reasoning")),
                             ("cache_read", cache.get("read")), ("cache_write", cache.get("write"))):
                    used[k] += int(v or 0)
                cost += float(part.get("cost") or 0)
            if ev.get("type") == "error":
                problem = ev.get("error") or {}
                error = ((problem.get("data") or {}).get("message")  # 1.x
                         or problem.get("message")  # 2.x
                         or json.dumps(problem)[:400])
        text = "\n".join(t for t in texts if t).strip()
        if timed_out:
            error = f"timed out after {req.timeout}s"
        ok = code == 0 and not error and bool(text)
        data = self.parse_free_json(text) if req.schema else None
        kind = ""
        if req.schema and data is None and ok:
            ok, error, kind = False, "no structured JSON in reply", OUTPUT
        return RunResult(ok, text, session_id, data,
                         None if ok else (error or err.strip()[-800:] or f"exit {code}"), dur, raw=out, kind=kind,
                         cost_usd=cost or None, tokens=token_counts(**used))

    def activity(self, ev, cwd):
        part = ev.get("part") or {}
        if part.get("type") == "tool":
            state = part.get("state") or {}
            inp = state.get("input") or {}
            arg = inp.get("filePath") or inp.get("command") or inp.get("pattern") or inp.get("path") or ""
            return tool_line(str(part.get("tool", "tool")).capitalize(), arg, cwd)
        return None

    def list_models(self) -> List[ModelInfo]:
        """1.x: from `opencode models --verbose`: a `provider/model` line, then that model's metadata as JSON, whose
        `variants` are the effort levels it supports (they differ per model: low/high/max, or none...xhigh).
        2.x: `opencode models` lists ids only."""
        if not self._binary:
            return []
        res = run_process([self._binary, "models", "--verbose"], cwd=os.getcwd(), timeout=90)
        out, head, body = [], None, []

        def flush():
            if head is None:
                return
            try:
                meta = json.loads("\n".join(body)) if body else {}
            except ValueError:
                meta = {}
            out.append(ModelInfo(head, meta.get("name") or "", sort_levels(list((meta.get("variants") or {}))),
                                 head.split("/", 1)[0]))
        for line in res.out.splitlines():
            if re.match(r"^[\w.-]+/\S+$", line.strip()) and not body or (body and body[-1].strip() == "}" and
                                                                          re.match(r"^[\w.-]+/\S+$", line.strip())):
                flush()
                head, body = line.strip(), []
            elif head is not None:
                body.append(line)
        flush()
        if not out:  # OpenCode 2.x (no --verbose) or an older 1.x: ids only, effort levels unknown
            plain = run_process([self._binary, "models"], cwd=os.getcwd(), timeout=60)
            out = [ModelInfo(l.strip(), "", [], l.strip().split("/", 1)[0]) for l in plain.out.splitlines()
                   if re.match(r"^[\w.-]+/\S+$", l.strip())]
        return out

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
