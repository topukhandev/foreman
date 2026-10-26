"""/btw: a quick side question answered on the spot, read-only, never saved in the conversation, also while a task
runs (the task is left alone)."""

import subprocess
import time
import unittest
from unittest import mock

from helpers import Script, Workspace, make_engine
from test_chat import InProcessChat

from orchestrator.brain import BTW_SCHEMA


class BtwTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.s = Script()
        self.eng = make_engine(self.ws, self.s)
        self.chat = InProcessChat(self.ws.api, engine=self.eng)

    def tearDown(self):
        self.ws.cleanup()

    def test_answered_read_only_without_a_task_and_not_saved(self):
        prompts = []
        self.s.btw = lambda req: (prompts.append(req), {"answer": "**42** rows."})[1]
        before = len(self.eng.store.all())
        self.chat._exchange("/btw how many rows are there?")
        self.assertEqual(len(self.eng.store.all()), before, "no task")
        self.assertEqual(prompts[0].mode, "read")
        self.assertIs(prompts[0].schema, BTW_SCHEMA)
        self.assertIn("how many rows are there?", prompts[0].prompt)
        self.assertEqual(self.chat.history.data["entries"], [], "not saved in the conversation")
        self.assertEqual(self.chat.turns, [])
        out = self.chat.respond("/btw how many rows?")
        self.assertIn("by the way: how many rows?", out)
        self.assertIn("42", out)

    def test_typed_while_a_task_runs_it_is_answered_there_and_the_task_goes_on(self):
        from orchestrator import chat as chat_module
        from orchestrator import ui
        task = self.eng.store.new_task("long job", self.ws.api)
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        keys = ["/btw what is running?\r"]
        said = []
        deadline = time.time() + 2

        class Reader:
            def read(self, timeout):
                time.sleep(0.05)
                return keys.pop(0) if keys else ""

            def close(self):
                pass

        class Watched:
            pid = proc.pid

            def poll(self):
                return None if time.time() < deadline else 0
        with mock.patch.object(chat_module, "_KeyReader", Reader), \
                mock.patch.object(ui.Spinner, "say", lambda _s, msg: said.append(msg)):
            self.chat._watch(task["id"], Watched())
        self.assertTrue(any("by the way: what is running?" in m and "42" in m for m in said), said)
        self.assertIsNone(proc.poll(), "the task kept running")
        self.assertEqual(self.chat.queue, [], "the side question is not sent as a message afterwards")


if __name__ == "__main__":
    unittest.main()
