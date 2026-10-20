import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))
"""Tests for Setup Wizard."""

import os
import tempfile
import unittest
import yaml
from orchestrator.wizard import run_setup_wizard, build_config_for_preset


class TestWizard(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_build_config_antigravity_only(self):
        cfg = build_config_for_preset("2", {})
        self.assertTrue(cfg["providers"]["antigravity_cli"]["enabled"])
        self.assertFalse(cfg["providers"]["claude_code"]["enabled"])

    def test_build_config_dual_model(self):
        cfg = build_config_for_preset("4", {})
        self.assertTrue(cfg["providers"]["antigravity_cli"]["enabled"])
        self.assertTrue(cfg["providers"]["claude_code"]["enabled"])

    def test_run_setup_wizard_non_interactive(self):
        out_file = run_setup_wizard(self.temp_dir.name, preset="6", non_interactive=True)
        self.assertTrue(os.path.exists(out_file))

        with open(out_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        self.assertTrue(data["providers"]["mock"]["enabled"])


if __name__ == "__main__":
    unittest.main()
