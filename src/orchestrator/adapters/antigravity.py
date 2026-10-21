"""Antigravity CLI worker adapter."""

import shutil
from typing import List, Optional
from orchestrator.adapters.base import BaseCLIAdapter
from orchestrator.finder import find_tool_executable


class AntigravityAdapter(BaseCLIAdapter):
    """Adapter for Antigravity CLI (agy / antigravity)."""

    def __init__(self, base_command: str = "agy -p", timeout_sec: int = 300, effort: str = "medium"):
        parts = base_command.strip().split()
        bin_name = parts[0] if parts else "agy"
        resolved = find_tool_executable(bin_name, ["agy", "antigravity"])
        if resolved:
            base_command = base_command.replace(bin_name, resolved, 1)
        super().__init__(name="antigravity_cli", base_command=base_command, timeout_sec=timeout_sec, effort=effort)

    def build_command(self, prompt: str, session_id: Optional[str] = None, extra_flags: Optional[List[str]] = None) -> List[str]:
        parts = self.base_command.strip().split()
        bin_path = parts[0] if parts else "agy"
        cmd = [bin_path]
        if extra_flags:
            cmd.extend(extra_flags)
        if getattr(self, "effort", None):
            cmd.append(f"--effort={self.effort}")
        if session_id:
            if session_id == "continue":
                cmd.append("--continue")
            else:
                cmd.append(f"--conversation={session_id}")
        # In agy CLI, attaching prompt as -p="<prompt>" prevents any argument parsing conflicts
        cmd.append(f"-p={prompt}")
        return cmd
