import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))
"""Tests for role packs (Developer, QA, Business Idea)."""

import unittest
from orchestrator.packs import PACKS, BusinessIdeaPack, DeveloperPack, QAPack


class TestPacks(unittest.TestCase):

    def test_packs_registered(self):
        self.assertIn("business_idea", PACKS)
        self.assertIn("developer", PACKS)
        self.assertIn("qa", PACKS)

    def test_developer_pack_tasks(self):
        tasks = DeveloperPack.get_tasks("Refactor database schema")
        self.assertEqual(len(tasks), 4)
        roles = [t.role for t in tasks]
        self.assertEqual(roles, ["planner", "critic", "coder", "reviewer"])
        # Check formatting prompt
        p = tasks[0].prompt_template.format(goal="Refactor DB", previous_output="")
        self.assertIn("Refactor DB", p)

    def test_qa_pack_tasks(self):
        tasks = QAPack.get_tasks("E2E tests for payment checkout")
        self.assertEqual(len(tasks), 4)
        roles = [t.role for t in tasks]
        self.assertEqual(roles, ["planner", "critic", "coder", "reviewer"])


if __name__ == "__main__":
    unittest.main()
