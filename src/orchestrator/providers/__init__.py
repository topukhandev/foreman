"""Provider factory. Add a provider by writing one module and registering its class here."""

from typing import Dict

from orchestrator.config import Config
from orchestrator.providers.antigravity import AntigravityProvider
from orchestrator.providers.base import (Capabilities, Provider, RunRequest, RunResult,  # noqa: F401
                                         SessionInfo)
from orchestrator.providers.claude import ClaudeProvider
from orchestrator.providers.opencode import OpenCodeProvider

PROVIDER_CLASSES = {
    "claude": ClaudeProvider,
    "opencode": OpenCodeProvider,
    "antigravity": AntigravityProvider,
}


def build_providers(cfg: Config) -> Dict[str, Provider]:
    """Instantiate configured providers (in preference order), skipping unknown names."""
    out: Dict[str, Provider] = {}
    for name, settings in cfg.providers.items():
        cls = PROVIDER_CLASSES.get(name)
        if cls:
            out[name] = cls(settings)
    return out
