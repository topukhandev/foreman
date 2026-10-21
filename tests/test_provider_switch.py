"""Unit tests for dynamic provider switching, state.md updating, and intent detection."""

import os
import shutil
import tempfile
import unittest
import yaml
from orchestrator.hub import Hub
from orchestrator.config import load_config, init_default_config_file
from orchestrator.repl import InteractiveShell


class TestProviderSwitching(unittest.TestCase):
    """Test provider switching, hub state synchronization, and intent handling."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.test_dir, "providers.yaml")
        init_default_config_file(self.config_path)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_hub_stale_task_cleanup(self):
        """Verify that stale 'running' tasks from dead sessions are cleaned up to idle."""
        hub = Hub(self.test_dir)
        # Write state with a running task
        hub.write_state(
            goal="Test Goal",
            phase="active",
            active_tasks=[{"id": "1", "title": "Stale", "role": "planner", "worker": "antigravity_cli", "status": "running"}],
            blockers=[]
        )
        self.assertIn("| running |", hub.read_state())

        # Now trigger cleanup
        cleaned = hub.cleanup_stale_active_tasks(default_provider="opencode")
        self.assertTrue(cleaned)

        state = hub.read_state()
        self.assertNotIn("| running |", state)
        self.assertIn("**Current Phase**: `idle`", state)
        self.assertIn("**Primary AI Provider**: `opencode`", state)
        self.assertIn("_No tasks currently active._", state)

    def test_provider_switch_updates_state_and_config(self):
        """Verify that switching provider in shell updates providers.yaml and state.md."""
        shell = InteractiveShell(self.test_dir)

        # Switch to mock
        shell.handle_provider_menu("mock")
        self.assertEqual(shell._get_active_provider_name(), "mock")

        state = shell.hub.read_state()
        self.assertIn("**Primary AI Provider**: `mock`", state)
        self.assertIn("**Current Phase**: `idle`", state)

    def test_detect_provider_intent_english_and_bengali(self):
        """Verify detection of user's natural language intent to switch provider."""
        shell = InteractiveShell(self.test_dir)

        # English specific
        self.assertEqual(shell._detect_provider_intent("switch provider to opencode"), "opencode")
        self.assertEqual(shell._detect_provider_intent("use claude"), "claude")
        self.assertEqual(shell._detect_provider_intent("change to antigravity"), "antigravity")
        self.assertEqual(shell._detect_provider_intent("switch to agy"), "antigravity")
        self.assertEqual(shell._detect_provider_intent("use mock"), "mock")

        # Generic switch (opens menu)
        self.assertEqual(shell._detect_provider_intent("change provider"), "")
        self.assertEqual(shell._detect_provider_intent("switch provider"), "")

        # Bengali phrases
        self.assertEqual(shell._detect_provider_intent("প্রোভাইডার চেঞ্জ করো"), "")
        self.assertEqual(shell._detect_provider_intent("প্রোভাইডার পরিবর্তন করতে চাই"), "")

        # Non-provider query
        self.assertIsNone(shell._detect_provider_intent("How do I write a Python function?"))

    def test_visual_toggle_command(self):
        """Verify /visual toggles visual_mode on and off."""
        shell = InteractiveShell(self.test_dir)
        initial = shell.visual_mode

        shell.handle_visual_toggle("off")
        self.assertFalse(shell.visual_mode)

        shell.handle_visual_toggle("on")
        self.assertTrue(shell.visual_mode)

        shell.handle_visual_toggle()
        self.assertEqual(shell.visual_mode, not True)


if __name__ == "__main__":
    unittest.main()
