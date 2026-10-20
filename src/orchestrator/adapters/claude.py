"""Claude Code CLI worker adapter."""

from typing import List, Optional
from orchestrator.adapters.base import BaseCLIAdapter
from orchestrator.finder import find_tool_executable


class ClaudeCodeAdapter(BaseCLIAdapter):
    """Adapter for Claude Code (claude -p)."""

    def __init__(self, base_command: str = "claude -p", timeout_sec: int = 300):
        parts = base_command.strip().split()
        bin_name = parts[0] if parts else "claude"
        resolved = find_tool_executable(bin_name, ["claude", "claude-code"])
        if resolved:
            base_command = base_command.replace(bin_name, resolved, 1)
        super().__init__(name="claude_code", base_command=base_command, timeout_sec=timeout_sec)

    def build_command(self, prompt: str, session_id: Optional[str] = None, extra_flags: Optional[List[str]] = None) -> List[str]:
        parts = self.base_command.strip().split()
        bin_path = parts[0] if parts else "claude"
        cmd = [bin_path]
        if extra_flags:
            cmd.extend(extra_flags)
        cmd.extend(["-p", prompt])
        return cmd
