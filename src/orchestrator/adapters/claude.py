"""Claude Code CLI worker adapter."""

from typing import List, Optional
from orchestrator.adapters.base import BaseCLIAdapter


class ClaudeCodeAdapter(BaseCLIAdapter):
    """Adapter for Claude Code (claude -p)."""

    def __init__(self, base_command: str = "claude -p", timeout_sec: int = 300):
        super().__init__(name="claude_code", base_command=base_command, timeout_sec=timeout_sec)

    def build_command(self, prompt: str, session_id: Optional[str] = None, extra_flags: Optional[List[str]] = None) -> List[str]:
        cmd_parts = self.base_command.strip().split()
        if extra_flags:
            cmd_parts.extend(extra_flags)
        cmd_parts.append(prompt)
        return cmd_parts
