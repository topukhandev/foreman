"""The application API (api.py), its event feed (feed.py) and the stdio JSON-RPC server (rpc_server.py), against
docs/api-contract.md. Runners are started in-process here; one test drives the real `foreman serve --stdio`."""

import io
import json
import os
import subprocess
import sys
import unittest

from helpers import Script, Workspace, make_engine, write

from orchestrator.api import Api, ApiError
from orchestrator.feed import Feed
from orchestrator.rpc_server import RpcServer

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_cli.py")
SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


def change_plan(needs_approval=False):
    return {"intent": "change", "title": "Footer", "projects": [{"name": "admin-ui", "role": "modify", "why": "x"}],
            "steps": [{"id": "1", "project": "admin-ui", "kind": "implement", "instructions": "add a footer",
                       "depends_on": []}], "questions": [],
            "impact": {"summary": "changes refunds", "risk": "high", "needs_approval": needs_approval}}


def edit(req):
    write(req.cwd, "src/footer.js", "// footer\n")
    return {"status": "done", "summary": "added the footer", "files_changed": ["src/footer.js"]}


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.s = Script()
        self.s.plan, self.s.step = lambda req: change_plan(), edit
        self.eng = make_engine(self.ws, self.s)
        self.started = []

        def spawn(task, plan_only):  # what a detached runner would do, here and now
            self.started.append((task["id"], plan_only))
            self.eng.plan_only(task) if plan_only else self.eng.execute(task)
        self.api = Api(self.eng, spawn=spawn)

    def tearDown(self):
        self.ws.cleanup()

    def kind(self, call, *a, **k) -> str:
        with self.assertRaises(ApiError) as ctx:
            call(*a, **k)
        return ctx.exception.kind

    def test_hello_and_version(self):
        hello = self.api.hello("desktop/test", "1.0")
        self.assertEqual((hello["api_version"], hello["actor"]), ("1.0", "desktop"))
        self.assertEqual(self.kind(self.api.hello, "x", "2.0"), "invalid")

    def test_submit_runs_the_task_in_a_runner_and_it_reads_back(self):
        res = self.api.tasks_submit("add a footer", self.ws.ui)
        self.assertEqual(self.started, [(res["task_id"], False)])
        got = self.api.tasks_get(res["task_id"])
        self.assertEqual(got["summary"]["status"], "done")
        self.assertGreater(got["summary"]["rev"], 1)
        self.assertFalse(got["summary"]["running"])
        self.assertIn("Footer", got["report_md"])
        self.assertEqual([t["id"] for t in self.api.tasks_list()["tasks"]], [res["task_id"]])
        created = self.eng.store.events(res["task_id"])[0]
        self.assertEqual((created["event"], created["actor"]), ("created", "desktop"))

    def test_instant_replies_and_bad_requests(self):
        self.assertIn("reply", self.api.tasks_submit("thanks", self.ws.ui))
        self.assertEqual(self.started, [])
        self.assertEqual(self.kind(self.api.tasks_submit, "", self.ws.ui), "invalid")
        self.assertEqual(self.kind(self.api.tasks_submit, "x", "/no/such/folder"), "invalid")
        self.assertEqual(self.kind(self.api.tasks_submit, "x", self.ws.ui, mode="nonsense"), "invalid")
        self.assertEqual(self.kind(self.api.tasks_get, "999"), "not_found")

    def test_a_dry_run_plans_in_the_runner_and_changes_nothing(self):
        res = self.api.tasks_submit("add a footer", self.ws.ui, dry_run=True)
        self.assertEqual(self.started, [(res["task_id"], True)])
        self.assertEqual(self.api.tasks_get(res["task_id"])["summary"]["status"], "planned")

    def test_the_same_request_twice_starts_one_task(self):
        a = self.api.tasks_submit("add a footer", self.ws.ui, idempotency_key="k1")
        b = self.api.tasks_submit("add a footer", self.ws.ui, idempotency_key="k1")
        self.assertEqual(a, b)
        self.assertEqual(len(self.started), 1)
        self.assertEqual(len(self.api.tasks_list()["tasks"]), 1)

    def test_approvals_need_the_plan_they_approve(self):
        self.s.plan = lambda req: change_plan(needs_approval=True)
        task_id = self.api.tasks_submit("change refunds", self.ws.ui)["task_id"]
        q = self.api.tasks_get(task_id)["summary"]["questions"][0]
        self.assertEqual(q["kind"], "approval")
        with self.assertRaises(ApiError) as ctx:
            self.api.tasks_answer(task_id, {q["id"]: "yes"}, plan_fp="0" * 16)
        self.assertEqual(ctx.exception.kind, "stale")
        self.assertEqual(ctx.exception.data["task"]["questions"][0]["plan_fp"], q["plan_fp"], "with the current plan")
        res = self.api.tasks_answer(task_id, {q["id"]: "yes"}, plan_fp=q["plan_fp"])
        self.assertTrue(res["queued"])
        self.assertEqual(self.api.tasks_get(task_id)["summary"]["status"], "done")

    def test_changes_to_a_running_task_are_refused(self):
        task = self.eng.store.new_task("x", self.ws.ui)
        task.update(status="needs_input", questions=[{"text": "Which colour?", "kind": "business", "answer": None}])
        self.eng.store.save(task)
        holder = subprocess.Popen([sys.executable, FAKE, "hold-lock", self.eng.store.path(task["id"], "run.lock"),
                                   task["id"]], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (holder.poll() is None and holder.kill(), holder.wait()))
        self.assertEqual(holder.stdout.readline().strip(), "ready")
        self.assertEqual(self.kind(self.api.tasks_answer, task["id"], {"q1": "blue"}), "running")
        self.assertEqual(self.kind(self.api.tasks_resume, task["id"]), "running")
        self.assertEqual(self.kind(self.api.tasks_close, task["id"]), "running")
        self.assertTrue(self.api.tasks_get(task["id"])["summary"]["running"])

    def test_close_stop_events_and_settings(self):
        task_id = self.api.tasks_submit("add a footer", self.ws.ui)["task_id"]
        self.assertEqual(self.api.tasks_stop(task_id), {"requested": False}, "nothing runs it")
        page = self.api.tasks_events(task_id, 0, limit=2)
        more = self.api.tasks_events(task_id, page["cursor"])
        self.assertEqual(len(page["events"]), 2)
        self.assertTrue(more["events"])
        self.assertTrue(self.api.tasks_events(task_id, 10 ** 9)["resync"])
        self.assertEqual(self.api.config_get()["on_close"], "ask")
        self.api.config_set_on_close("keep")
        self.assertEqual(self.api.config_get()["on_close"], "keep")
        self.assertEqual(self.kind(self.api.config_set_on_close, "maybe"), "invalid")
        self.assertIn("orders-api", [p["name"] for p in self.api.projects_list()["projects"]])
        ui = self.api.projects_get("admin-ui")
        self.assertIn("orders-api", [r["other"] for r in ui["relations"]])


class FeedTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.eng = make_engine(self.ws, Script())
        self.feed = Feed(self.eng)

    def tearDown(self):
        self.ws.cleanup()

    def test_follows_events_from_now_and_new_tasks_from_their_start(self):
        old = self.eng.store.new_task("old", self.ws.ui)
        old["status"] = "running"
        self.eng.store.save(old)
        self.feed.subscribe("active")
        self.eng.store.event(old["id"], "activity", text="Read(x)")
        new = self.eng.store.new_task("new", self.ws.ui)
        notes = self.feed.poll()
        events = [(p["task_id"], p["event"]["event"]) for m, p in notes if m == "event"]
        self.assertIn((old["id"], "activity"), events)
        self.assertNotIn((old["id"], "created"), events, "history is tasks.events' job")
        self.assertIn((new["id"], "created"), events, "a task that started later: from its beginning")
        self.assertIn(("tasks_changed", {"task_ids": [new["id"]]}), [n for n in notes if n[0] == "tasks_changed"])
        cursors = [p["cursor"] for m, p in notes if m == "event" and p["task_id"] == old["id"]]
        self.assertEqual(cursors, sorted(cursors))
        self.assertEqual([n for n in self.feed.poll() if n[0] == "event"], [], "nothing twice")

    def test_a_cursor_that_doesnt_fit_asks_for_a_resync(self):
        task = self.eng.store.new_task("x", self.ws.ui)
        self.feed.subscribe([task["id"]], since={task["id"]: 10 ** 6})
        self.assertEqual(self.feed.poll()[0][0], "resync")


class RpcTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.eng = make_engine(self.ws, Script())
        self.out = io.BytesIO()
        self.server = RpcServer(Api(self.eng, spawn=lambda task, plan_only: None), self.out)

    def tearDown(self):
        self.ws.cleanup()

    def rpc(self, method, params=None, mid=1):
        return self.server.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})

    def test_hello_first_then_methods_errors_and_params(self):
        self.assertEqual(self.rpc("tasks.list")["error"]["data"]["kind"], "invalid")
        self.assertEqual(self.rpc("system.hello", {"client": "t", "api_version": "1.0"})["result"]["api_version"], "1.0")
        self.assertEqual(self.rpc("tasks.list")["result"]["tasks"], [])
        self.assertEqual(self.rpc("tasks.get", {"task_id": "42"})["error"]["data"]["kind"], "not_found")
        self.assertEqual(self.rpc("tasks.get", {"nope": 1})["error"]["code"], -32602)
        self.assertEqual(self.rpc("no.such")["error"]["code"], -32601)
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "tasks.list"}), "a notification: no answer")

    def test_serve_answers_each_line_and_sends_utf8(self):
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "system.hello", "params": {"api_version": "1.0"}},
                 {"jsonrpc": "2.0", "id": 2, "method": "tasks.submit", "params": {"request": "thanks ✔", "cwd": self.ws.ui}}]
        inp = io.BytesIO(b"".join(json.dumps(m, ensure_ascii=False).encode() + b"\n" for m in lines) + b"{bad\n")
        self.server.serve(inp)
        replies = [json.loads(l) for l in self.out.getvalue().decode("utf-8").splitlines()]
        self.assertEqual(sorted(r.get("id") or 0 for r in replies), [0, 1, 2])
        self.assertTrue(any(r.get("error", {}).get("code") == -32700 for r in replies), "a bad line: parse error")


class StdioProcessTest(unittest.TestCase):
    def test_the_real_server_speaks_only_protocol_on_stdout_and_ends_with_stdin(self):
        ws = Workspace()
        try:
            env = {**os.environ, "FOREMAN_HOME": ws.home, "PYTHONPATH": SRC}
            proc = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "orchestrator.cli", "serve", "--stdio"],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            reqs = [{"jsonrpc": "2.0", "id": 1, "method": "system.hello", "params": {"api_version": "1.0"}},
                    {"jsonrpc": "2.0", "id": 2, "method": "projects.list"}]
            out, err = proc.communicate(b"".join(json.dumps(r).encode() + b"\n" for r in reqs), timeout=120)
            self.assertEqual(proc.returncode, 0, err.decode(errors="replace")[-2000:])
            replies = {r["id"]: r for r in (json.loads(l) for l in out.decode("utf-8").splitlines())}
            self.assertEqual(set(replies), {1, 2}, "every stdout line is a protocol message")
            self.assertEqual(replies[1]["result"]["actor"], "desktop")
            self.assertIn("admin-ui", [p["name"] for p in replies[2]["result"]["projects"]])
        finally:
            ws.cleanup()


if __name__ == "__main__":
    unittest.main()
