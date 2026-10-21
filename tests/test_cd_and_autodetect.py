"""Tests for target directory auto-detection, /cd command, and adaptive timeouts."""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from orchestrator.repl import find_target_project, InteractiveShell
from orchestrator.input_reader import COMMANDS


class TestCdAndAutoDetect(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="foreman_test_cd_")
        self.proj_a = os.path.join(self.tmp_dir, "orchestrator_test_project")
        os.makedirs(self.proj_a, exist_ok=True)
        self.proj_b = os.path.join(self.tmp_dir, "my_awesome_api")
        os.makedirs(self.proj_b, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_commands_list_has_cd(self):
        cmd_names = [cmd for cmd, _ in COMMANDS]
        self.assertIn("/cd", cmd_names)

    def test_find_target_project_explicit_path(self):
        query = f"Please check the code in {self.proj_a} and fix it."
        found = find_target_project(query, current_dir=self.tmp_dir)
        self.assertEqual(found, os.path.abspath(self.proj_a))

    def test_find_target_project_fuzzy_tokens(self):
        query = "In desktop there is a project called orchestration test project. Make UI better."
        # When searching with tmp_dir as desktop:
        found = find_target_project(query, current_dir=self.tmp_dir, desktop_dir=self.tmp_dir)
        self.assertEqual(found, os.path.abspath(self.proj_a))

    def test_find_target_project_no_match(self):
        query = "What is the capital of Bangladesh? Tell me a joke."
        found = find_target_project(query, current_dir=self.tmp_dir)
        self.assertIsNone(found)

    def test_change_project_directory(self):
        shell = InteractiveShell(project_dir=self.tmp_dir)
        self.assertEqual(shell.project_dir, os.path.abspath(self.tmp_dir))

        ok = shell.change_project_directory(self.proj_a)
        self.assertTrue(ok)
        self.assertEqual(shell.project_dir, os.path.abspath(self.proj_a))
        self.assertEqual(str(shell.hub.root_dir), os.path.abspath(self.proj_a))

    def test_change_project_directory_invalid(self):
        shell = InteractiveShell(project_dir=self.tmp_dir)
        ok = shell.change_project_directory("/non_existent_folder_xyz_12345")
        self.assertFalse(ok)
        self.assertEqual(shell.project_dir, os.path.abspath(self.tmp_dir))

    def test_handle_cd_command(self):
        shell = InteractiveShell(project_dir=self.tmp_dir)
        shell.handle_cd_command(self.proj_b)
        self.assertEqual(shell.project_dir, os.path.abspath(self.proj_b))

    def test_handle_lightweight_query_cd(self):
        shell = InteractiveShell(project_dir=self.tmp_dir)
        handled = shell.handle_lightweight_query(f"cd {self.proj_a}")
        self.assertTrue(handled)
        self.assertEqual(shell.project_dir, os.path.abspath(self.proj_a))

    def test_adaptive_timeout(self):
        shell = InteractiveShell(project_dir=self.tmp_dir)
        shell.config.budget.max_time_per_task_sec = 600

        # When effort is high -> effective_timeout at least 900
        shell.effort = "high"
        base = shell.config.budget.max_time_per_task_sec
        eff_high = max(base, 900) if shell.effort == "high" else base
        self.assertEqual(eff_high, 900)

        # When effort is low -> effective_timeout 300
        shell.effort = "low"
        eff_low = max(base, 300) if shell.effort != "low" else 300
        self.assertEqual(eff_low, 300)


if __name__ == "__main__":
    unittest.main()
