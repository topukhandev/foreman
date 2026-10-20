"""Antigravity CLI worker adapter."""

import shutil
from typing import List, Optional
from orchestrator.adapters.base import BaseCLIAdapter


class AntigravityAdapter(BaseCLIAdapter):
    """Adapter for Antigravity CLI (agy / antigravity)."""

    def __init__(self, base_command: str = "agy -p", timeout_sec: int = 300):
        # Fall back to antigravity binary if agy is not directly in PATH
        parts = base_command.strip().split()
        bin_name = parts[0] if parts else "agy"
        if not shutil.which(bin_name) and shutil.which("antigravity"):
            base_command = base_command.replace(bin_name, "antigravity", 1)
        super().__init__(name="antigravity_cli", base_command=base_command, timeout_sec=timeout_sec)

    def build_command(self, prompt: str, session_id: Optional[str] = None, extra_flags: Optional[List[str]] = None) -> List[str]:
        cmd_parts = self.base_command.strip().split()
        if extra_flags:
            cmd_parts.extend(extra_flags)
        if session_id:
            cmd_parts.extend(["--conversation", session_id])
        cmd_parts.append(prompt)
        return cmd_parts
