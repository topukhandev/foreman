"""OpenCode CLI worker adapter."""

from typing import List, Optional
from orchestrator.adapters.base import BaseCLIAdapter
from orchestrator.finder import find_tool_executable


class OpenCodeAdapter(BaseCLIAdapter):
    """Adapter for OpenCode CLI (opencode run)."""

    def __init__(self, base_command: str = "opencode run", timeout_sec: int = 300):
        parts = base_command.strip().split()
        bin_name = parts[0] if parts else "opencode"
        sub_cmd = parts[1:] if len(parts) > 1 else ["run"]
        resolved = find_tool_executable(bin_name, ["opencode", "opencode-cli", "open-code"])
        if resolved:
            base_command = f"{resolved} {' '.join(sub_cmd)}"
        super().__init__(name="opencode", base_command=base_command, timeout_sec=timeout_sec)

    def build_command(self, prompt: str, session_id: Optional[str] = None, extra_flags: Optional[List[str]] = None) -> List[str]:
        cmd_parts = self.base_command.strip().split()
        if extra_flags:
            cmd_parts.extend(extra_flags)
        if session_id:
            if session_id == "continue":
                cmd_parts.append("--continue")
            else:
                cmd_parts.extend(["--session", session_id])
        cmd_parts.append(prompt)
        return cmd_parts
