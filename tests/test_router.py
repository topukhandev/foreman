import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))
"""Tests for Router and reviewer independence."""

import unittest
from orchestrator.config import ProviderConfig, RoutingConfig
from orchestrator.router import Router


class TestRouter(unittest.TestCase):

    def test_routing_with_multiple_providers(self):
        providers = {
            "antigravity_cli": ProviderConfig(
                name="antigravity_cli",
                enabled=True,
                verified=True,
                command="mock",
                roles=["planner", "coder"]
            ),
            "claude_code": ProviderConfig(
                name="claude_code",
                enabled=True,
                verified=True,
                command="mock",
                roles=["reviewer", "critic"]
            )
        }
        routing = RoutingConfig(reviewer_must_differ_from_builder=True)
        router = Router(providers, routing)

        # Worker selection
        worker = router.select_worker("coder")
        self.assertIsNotNone(worker)
        self.assertEqual(worker[0], "antigravity_cli")

        # Reviewer selection must differ from builder
        reviewer = router.select_reviewer("antigravity_cli")
        self.assertIsNotNone(reviewer)
        rev_provider, session_id, is_distinct = reviewer
        self.assertEqual(rev_provider, "claude_code")
        self.assertTrue(is_distinct)

    def test_single_provider_fresh_session_fallback(self):
        providers = {
            "antigravity_cli": ProviderConfig(
                name="antigravity_cli",
                enabled=True,
                verified=True,
                command="mock",
                roles=["planner", "coder", "reviewer"]
            )
        }
        routing = RoutingConfig(reviewer_must_differ_from_builder=True)
        router = Router(providers, routing)

        reviewer = router.select_reviewer("antigravity_cli")
        self.assertIsNotNone(reviewer)
        rev_provider, session_id, is_distinct = reviewer
        self.assertEqual(rev_provider, "antigravity_cli")
        self.assertFalse(is_distinct)
        self.assertTrue(session_id.startswith("fresh_review_"))


if __name__ == "__main__":
    unittest.main()
