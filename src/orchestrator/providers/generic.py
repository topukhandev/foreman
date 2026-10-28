"""Any other AI CLI, configured in config.yaml without code:

    providers:
      codex:
        type: generic
        command: codex
        read_args: [exec, --sandbox, read-only, --skip-git-repo-check, "{prompt}"]
        write_args: [exec, --sandbox, workspace-write, --skip-git-repo-check, "{prompt}"]
        read_only_enforced: true    # only if the CLI itself blocks edits in read mode
        web: false                  # whether read_args allow web research

Placeholders: {prompt}, {cwd}. The process runs in the project directory. Output is treated as plain text; the
structured result is read from a fenced JSON block the prompt asks for. No session reuse, so every step starts
fresh. Foreman's own guards (file-change checks on read steps, the HEAD check) apply as for every provider.
"""

from dataclasses import replace
from typing import List

from orchestrator.providers.base import GIT_POLICY, Capabilities, Provider, RunRequest, RunResult, fit_prompt


class GenericProvider(Provider):
    name = "generic"

    def __init__(self, settings):
        super().__init__(settings)
        self.name = settings.name
        extra = settings.extra or {}
        self.capabilities = Capabilities(structured_output=False, enforced_read_only=bool(extra.get("read_only_enforced")),
                                         web=bool(extra.get("web")))

    def build_command(self, req: RunRequest) -> List[str]:
        extra = self.settings.extra or {}
        args = extra.get("write_args" if req.mode == "write" else "read_args") or ["{prompt}"]
        prompt = req.prompt
        if req.extra_dirs:
            prompt += "\n\nRelated project directories you may read: " + ", ".join(req.extra_dirs)
        prompt += "\n\n" + (GIT_POLICY if req.mode == "write" else
                            "Read-only step: do not create, modify or delete files.")
        if req.schema:
            prompt += self.schema_instruction(req.schema)
        prompt = fit_prompt(replace(req, prompt=prompt), self.batch_shim).prompt  # everything, in a file if need be
        return [self._binary or self.settings.command] + [str(a).replace("{prompt}", prompt).replace("{cwd}", req.cwd)
                                                          for a in args]

    def run(self, req: RunRequest) -> RunResult:
        code, out, err, dur, timed_out = self._exec(self.build_command(req), req)
        if timed_out:
            return RunResult(False, error=f"timed out after {req.timeout}s", duration=dur, raw=out)
        text = out.strip()
        data = self.parse_free_json(text) if req.schema else None
        ok = code == 0 and bool(text) and (data is not None or not req.schema)
        error = None if ok else ((err.strip() or text)[-800:] or f"exit {code}")
        if code == 0 and req.schema and data is None:
            error = "no structured JSON in reply"
        return RunResult(ok, text, None, data, error, dur, raw=out)
