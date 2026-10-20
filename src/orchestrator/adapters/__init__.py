"""Adapters registry and factory for AI CLI workers."""

from typing import Dict, Optional
from orchestrator.adapters.base import BaseCLIAdapter, ExecutionResult
from orchestrator.adapters.antigravity import AntigravityAdapter
from orchestrator.adapters.claude import ClaudeCodeAdapter
from orchestrator.adapters.opencode import OpenCodeAdapter
from orchestrator.adapters.mock import MockAdapter
from orchestrator.config import ProviderConfig


def create_adapter(provider_name: str, config: ProviderConfig, timeout_sec: int = 300) -> BaseCLIAdapter:
    """Factory to instantiate the appropriate CLI adapter."""
    cmd = config.command.strip()
    if provider_name == "antigravity_cli":
        return AntigravityAdapter(base_command=cmd or "agy -p", timeout_sec=timeout_sec)
    elif provider_name == "claude_code":
        return ClaudeCodeAdapter(base_command=cmd or "claude -p", timeout_sec=timeout_sec)
    elif provider_name == "opencode":
        return OpenCodeAdapter(base_command=cmd or "opencode run", timeout_sec=timeout_sec)
    elif provider_name == "mock" or cmd.startswith("mock"):
        return MockAdapter(name=provider_name, timeout_sec=timeout_sec)
    else:
        # Fallback to base adapter using the provided command
        return BaseCLIAdapter(name=provider_name, base_command=cmd, timeout_sec=timeout_sec)
