"""Task state that several processes share: task.json is the authority (with rev), events.jsonl is an append-only
log read from byte cursors, shared files are updated under a cross-process lock, and a damaged record is reported
instead of disappearing."""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest

import helpers  # noqa: F401  (puts src/ on the path)

from orchestrator.budget import Budget
from orchestrator.store import Store

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


class EventLogTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(tempfile.mkdtemp(prefix="foreman_state_"))
        self.task = self.store.new_task("x", tempfile.gettempdir())
        self.log = self.store.path(self.task["id"], "events.jsonl")

    def test_a_line_cut_short_by_a_crash_never_swallows_the_next_event(self):
        with open(self.log, "ab") as f:
            f.write(b'{"at": "x", "event": "acti')  # the runner died mid-write
        self.store.event(self.task["id"], "after_the_crash", n=1)
        self.store.event(self.task["id"], "later", n=2)
        kinds = [e["event"] for e in self.store.events(self.task["id"])]
        self.assertEqual(kinds, ["created", "after_the_crash", "later"])

    def test_events_carry_an_unambiguous_timestamp(self):
        ev = self.store.events(self.task["id"])[0]
        self.assertIsInstance(ev["ts"], float)
        self.assertIn("at", ev)

    def test_readers_take_complete_lines_and_resume_from_their_cursor(self):
        self.store.event(self.task["id"], "one")
        events, cursor, resync = self.store.read_events(self.task["id"], 0)
        self.assertFalse(resync)
        self.assertEqual([e["event"] for e in events], ["created", "one"])
        self.assertEqual(events[-1]["cursor"], cursor)
        with open(self.log, "ab") as f:
            f.write(b'{"event": "two", "at": "')  # being written right now
        events, again, _ = self.store.read_events(self.task["id"], cursor)
        self.assertEqual((events, again), ([], cursor), "a partial line is not read yet")
        with open(self.log, "ab") as f:
            f.write(b'now"}\n')
        events, _, _ = self.store.read_events(self.task["id"], cursor)
        self.assertEqual([e["event"] for e in events], ["two"])

    def test_a_cursor_that_doesnt_fit_the_log_asks_for_a_resync(self):
        size = os.path.getsize(self.log)
        for bad in (size + 100, 3):  # past the end; in the middle of a line
            events, cursor, resync = self.store.read_events(self.task["id"], bad)
            self.assertTrue(resync)
            self.assertEqual((events, cursor), ([], size))

    def test_replay_while_a_runner_writes_has_no_gaps_and_no_duplicates(self):
        def writer():
            for n in range(400):
                self.store.event(self.task["id"], "activity", n=n)
        t = threading.Thread(target=writer)
        t.start()
        seen, cursor = [], 0

        def read() -> bool:
            nonlocal cursor
            events, cursor, resync = self.store.read_events(self.task["id"], cursor, limit=37)
            self.assertFalse(resync)
            seen.extend(e["n"] for e in events if e["event"] == "activity")
            return bool(events)
        while t.is_alive():  # reading while it writes
            read()
        t.join()
        while read():  # then what came after the last read
            pass
        self.assertEqual(seen, list(range(400)))


class RecordTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(tempfile.mkdtemp(prefix="foreman_state_"))

    def test_every_save_raises_rev_and_announces_what_readers_act_on(self):
        task = self.store.new_task("x", tempfile.gettempdir())
        rev = task["rev"]
        task["status"] = "running"
        task["questions"].append({"text": "Which colour?", "kind": "business", "answer": None})
        task["notices"].append("claude reached its usage limit")
        self.store.save(task)
        self.assertEqual(task["rev"], rev + 1)
        other = Store(self.store.home)  # another process, starting from the record on disk
        again = other.load(task["id"])
        again["status"] = "interrupted"
        other.save(again, reason="runner_exited")
        self.assertEqual(again["rev"], rev + 2)
        ev = [e for e in self.store.events(task["id"]) if e["event"] in ("status", "question_opened", "notice")]
        self.assertEqual([(e["event"], e.get("status") or e.get("question") or e.get("text")) for e in ev],
                         [("status", "running"), ("question_opened", "q1"), ("notice", "claude reached its usage limit"),
                          ("status", "interrupted")])
        self.assertEqual((ev[0]["previous"], ev[-1]["previous"], ev[-1]["reason"]), ("planning", "running", "runner_exited"))

    def test_a_damaged_record_is_listed_not_hidden(self):
        good = self.store.new_task("fine", tempfile.gettempdir())
        bad = self.store.new_task("damaged", tempfile.gettempdir())
        with open(self.store.path(bad["id"], "task.json"), "w") as f:
            f.write('{"id": "2", "title": ')  # e.g. a power loss
        self.assertEqual([t["id"] for t in self.store.all()], [good["id"]])
        self.assertEqual(self.store.unreadable(), [bad["id"]])


WORKER = textwrap.dedent('''
    import sys
    sys.path.insert(0, {src!r})
    from orchestrator.budget import Budget
    from orchestrator.store import Store
    store, budget = Store({home!r}), Budget({home!r}, {{"claude": 0}})
    for n in range(25):
        store.add_suggestions("1", [{{"text": "suggestion {who} " + str(n), "impact": "major"}}])
        budget.acquire("claude")
''')


class SharedFileTest(unittest.TestCase):
    def test_two_processes_never_lose_each_others_updates(self):
        home = tempfile.mkdtemp(prefix="foreman_shared_")
        procs = [subprocess.Popen([sys.executable, "-c", WORKER.format(src=SRC, home=home, who=w)]) for w in "ab"]
        for p in procs:
            self.assertEqual(p.wait(120), 0)
        items = Store(home).suggestions()
        self.assertEqual(len(items), 50)
        self.assertEqual(len({s["id"] for s in items}), 50, "unique ids")
        self.assertEqual(Budget(home, {"claude": 0}).used("claude"), 50, "every run counted")
        with open(os.path.join(home, "suggestions.json")) as f:
            json.load(f)


if __name__ == "__main__":
    unittest.main()
