"""Tests for File Hub protocol."""

import os
import tempfile
import unittest
from orchestrator.hub import Hub, InboxMessage


class TestHub(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.hub = Hub(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_hub_directories_created(self):
        self.assertTrue(self.hub.inbox_dir.exists())
        self.assertTrue(self.hub.tasks_dir.exists())
        self.assertTrue(self.hub.status_dir.exists())
        self.assertTrue(self.hub.logs_dir.exists())
        self.assertTrue(self.hub.archive_dir.exists())
        self.assertTrue(self.hub.state_file.exists())

    def test_inbox_message_exchange(self):
        msg = InboxMessage(
            msg_id="msg_001",
            sender="manager",
            recipient="worker:antigravity_cli",
            task_id="task_1",
            msg_type="assign",
            body="Review unit economics"
        )
        self.hub.send_message(msg)

        msgs = self.hub.get_messages(recipient="worker:antigravity_cli")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].task_id, "task_1")
        self.assertEqual(msgs[0].msg_type, "assign")

    def test_state_file_written(self):
        self.hub.write_state(
            goal="Test Goal",
            phase="active",
            active_tasks=[{"id": "1", "title": "Checklist", "role": "planner", "worker": "mock", "running_sec": 5, "status": "running"}],
            blockers=["Need approval"]
        )
        content = self.hub.read_state()
        self.assertIn("Test Goal", content)
        self.assertIn("Checklist", content)
        self.assertIn("Need approval", content)

    def test_task_archival(self):
        self.hub.archive_task("task_99", "Successfully evaluated market size")
        master_archive = self.hub.archive_dir / "index.md"
        self.assertTrue(master_archive.exists())
        with open(master_archive, "r", encoding="utf-8") as f:
            data = f.read()
        self.assertIn("task_99", data)


if __name__ == "__main__":
    unittest.main()
