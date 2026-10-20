"""Role-to-provider routing with reviewer isolation and fallback logic."""

import uuid
from typing import Dict, List, Optional, Tuple
from orchestrator.config import ProviderConfig, RoutingConfig


class Router:
    """Routes role requirements to verified local providers while enforcing review independence."""

    def __init__(self, providers: Dict[str, ProviderConfig], routing_config: RoutingConfig):
        self.providers = providers
        self.routing_config = routing_config

    def get_enabled_providers(self) -> List[ProviderConfig]:
        """Return list of enabled and verified providers."""
        return [p for p in self.providers.values() if p.enabled and p.verified]

    def select_worker(self, role: str) -> Optional[Tuple[str, str]]:
        """
        Select a provider for a specific role.
        Returns: (provider_name, session_id) or None if no provider available.
        """
        enabled = self.get_enabled_providers()
        if not enabled:
            return None

        # 1. Match role exactly
        matching = [p for p in enabled if role in p.roles]
        if matching:
            # Pick first available matching provider
            chosen = matching[0]
            session_id = f"{role}_{uuid.uuid4().hex[:6]}"
            return chosen.name, session_id

        # 2. Fallback if allowed
        if self.routing_config.fallback == "any_enabled" and enabled:
            chosen = enabled[0]
            session_id = f"{role}_{uuid.uuid4().hex[:6]}"
            return chosen.name, session_id

        return None

    def select_reviewer(self, builder_provider: str) -> Optional[Tuple[str, str, bool]]:
        """
        Select an independent reviewer.
        Returns: (reviewer_provider, session_id, is_distinct_provider) or None.
        Enforces blueprint rule: different provider if available, otherwise fresh clean session.
        """
        enabled = self.get_enabled_providers()
        if not enabled:
            return None

        # 1. Look for a different provider supporting reviewer role
        if self.routing_config.reviewer_must_differ_from_builder:
            different_providers = [
                p for p in enabled
                if p.name != builder_provider and ("reviewer" in p.roles or "critic" in p.roles)
            ]
            if different_providers:
                chosen = different_providers[0]
                session_id = f"rev_{uuid.uuid4().hex[:6]}"
                return chosen.name, session_id, True

        # 2. If no different provider is available, use builder provider with a completely fresh session
        builder = self.providers.get(builder_provider)
        if builder and builder.enabled and builder.verified:
            fresh_session_id = f"fresh_review_{uuid.uuid4().hex[:8]}"
            return builder.name, fresh_session_id, False

        # 3. Fallback to any enabled provider with fresh session
        chosen = enabled[0]
        fresh_session_id = f"fresh_review_{uuid.uuid4().hex[:8]}"
        is_distinct = (chosen.name != builder_provider)
        return chosen.name, fresh_session_id, is_distinct
