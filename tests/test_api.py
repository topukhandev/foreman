"""The application API (api.py), its event feed (feed.py) and the stdio JSON-RPC server (rpc_server.py), against
docs/api-contract.md. Runners are started in-process here; one test drives the real `foreman serve --stdio`."""

import contextlib
import io
import json
import os
import subprocess
import sys
import unittest

from helpers import Script, Workspace, make_engine, write

from orchestrator.api import Api, ApiError
from orchestrator.feed import Feed
from orchestrator.mcp_server import MCPServer
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
        self.assertEqual((hello["api_version"], hello["actor"]), ("1.5", "desktop"))
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


    def test_settings_are_the_terminal_menus_and_save_the_same_way(self):
        from orchestrator.providers.base import ModelInfo
        got = self.api.config_get()
        keys = [x["key"] for x in got["settings"]]
        self.assertEqual(keys[:3], ["display", "auto_switch", "planner"])
        self.assertIn("on_close", keys)
        self.assertEqual(self.api.config_set("auto_switch", False)["setting"]["value"], False)
        self.assertFalse(self.eng.cfg.auto_switch)
        self.api.config_set("output_style", "concise")
        self.assertEqual(self.eng.cfg.output_style, "concise")
        self.assertEqual(self.kind(self.api.config_set, "output_style", "loud"), "invalid")
        self.assertEqual(self.kind(self.api.config_set, "nonsense", True), "invalid")
        first = [o["value"] for o in got["settings"][3]["options"]][-1]
        self.api.config_set("first_worker", first)
        self.assertEqual(next(iter(self.eng.providers)), first)

        name = next(iter(self.eng.providers))
        self.eng.providers[name].list_models = lambda: [ModelInfo("big", "Big", ["low", "high", "max"]),
                                                        ModelInfo("small", "Small", [])]
        listed = self.api.models_list(name)
        self.assertEqual([m["id"] for m in listed["models"]], ["big", "small"])
        self.assertEqual(self.api.models_set(name, "big", "max"), {"provider": name, "model": "big", "effort": "max"})
        self.assertEqual((self.api.config_get()["providers"][name]["model"], self.eng.cfg.providers[name].chosen().effort),
                         ("big", "max"))
        self.assertEqual(self.api.models_set(name, "big")["effort"], "high", "no effort given: high")
        self.assertIsNone(self.api.models_set(name, "small")["effort"], "a model without levels has no effort")
        self.assertEqual(self.kind(self.api.models_set, name, "big", "ultra"), "invalid")
        self.assertEqual(self.kind(self.api.models_list, "nobody"), "not_found")
        self.assertEqual(self.api.models_set(name, "default")["model"], "default")


    def test_a_conversation_keeps_its_tasks_and_answers_their_questions(self):
        self.s.plan = lambda req: change_plan(needs_approval=True)
        first = self.api.conversations_send(self.ws.ui, "change refunds")
        cid, project, task_id = first["conversation_id"], first["project"], first["task_id"]
        self.assertEqual(project, "admin-ui")
        self.assertEqual(self.eng.store.load(task_id)["status"], "needs_input")
        reply = self.api.conversations_send(self.ws.ui, f"what's the status of #{task_id}?", conversation_id=cid)
        self.assertNotIn("task_id", reply, "a status question is an instant reply, not an answer")
        self.assertIn(f"#{task_id}", reply["reply"])
        self.assertEqual(self.eng.store.load(task_id)["status"], "needs_input", "and doesn't touch the task")
        # a plain "yes" while the conversation's task waits for its approval answers that question
        self.s.plan = lambda req: change_plan()
        yes = self.api.conversations_send(self.ws.ui, "yes", conversation_id=cid)
        self.assertEqual(yes["task_id"], task_id)
        self.assertEqual(self.eng.store.load(task_id)["status"], "done")

        listed = self.api.conversations_list()["conversations"]
        self.assertEqual([(c["id"], c["project"], c["tasks"], c["messages"]) for c in listed], [(cid, "admin-ui", [task_id], 3)])
        self.assertEqual(listed[0]["title"], "change refunds")
        got = self.api.conversations_get(project, cid)
        self.assertEqual([e.get("task") for e in got["conversation"]["entries"]], [task_id, None, task_id])
        self.assertEqual(got["tasks"][task_id]["status"], "done")
        self.assertEqual(got["tasks"][task_id]["project"], "admin-ui")
        self.assertEqual(self.api.conversations_rename(project, cid, "Refunds")["title"], "Refunds")
        self.assertEqual(self.kind(self.api.conversations_get, project, "nope"), "not_found")
        self.assertEqual(self.kind(self.api.conversations_get, project, "../../etc/passwd"), "not_found")
        self.assertEqual(self.kind(self.api.conversations_send, self.ws.ui, " "), "invalid")

    def test_a_terminal_conversation_opens_in_the_app_and_the_other_way_round(self):
        from orchestrator.conversations import ChatHistory
        terminal = ChatHistory(self.eng.cfg.home, "admin-ui")
        terminal.append("how are totals shown?", "In show().", [["how are totals shown?", "In show()."]], None)
        got = self.api.conversations_get("admin-ui", terminal.chat_id)
        self.assertEqual(got["conversation"]["entries"][0]["output"], "In show().")
        self.api.conversations_send(self.ws.ui, "thanks", conversation_id=terminal.chat_id)
        again = ChatHistory(self.eng.cfg.home, "admin-ui")
        self.assertEqual(len(again.load(terminal.chat_id)["entries"]), 2, "the terminal sees the app's message")


class LiveOutputTest(unittest.TestCase):
    SECRET = "sk-ant-api03-livesecretvalue-0123456789"

    def setUp(self):
        self.ws = Workspace()
        self.s = Script()
        self.s.plan = lambda req: {**change_plan(), "impact": {"summary": "a footer", "risk": "low"}}  # one run

        def noisy(req):  # a CLI printing as it works: colours, a key it read, a token-looking value
            for line in ["\x1b[32mReading src\x1b[0m", "-----BEGIN RSA PRIVATE KEY-----", "MIIEowIBAAKCAQEAsecret",
                         "-----END RSA PRIVATE KEY-----", f"token {self.SECRET} done", "ghp_" + "a" * 36,
                         "evil \x1b]52;c;cGF5bG9hZA==\x07 \u202ereversed"]:
                req.on_output(line + "\n")
            return edit(req)
        self.s.step = noisy
        self.eng = make_engine(self.ws, self.s)
        self.api = Api(self.eng, spawn=lambda task, plan_only: self.eng.execute(task))

    def tearDown(self):
        self.ws.cleanup()

    def test_output_is_stored_redacted_and_served_as_plain_text(self):
        task_id = self.api.tasks_submit("add a footer", self.ws.ui)["task_id"]
        outs = self.api.tasks_outputs(task_id)["outputs"]
        self.assertEqual(len(outs), 1)
        name = outs[0]["name"]
        self.assertTrue(name.startswith("step-") and name.endswith("-1-claude"), name)  # step, attempt, provider
        self.assertIn(name, [e.get("log") for e in self.eng.store.events(task_id) if e["event"] == "output"])
        with open(self.eng.store.path(task_id, "logs", name + ".live"), encoding="utf-8") as f:
            stored = f.read()
        for secret in ("MIIEowIBAAKCAQEAsecret", self.SECRET, "ghp_aaaa"):
            self.assertNotIn(secret, stored, "redacted before it reached the disk")
        self.assertIn("[redacted private key]", stored)
        self.assertIn("[redacted token]", stored)
        got = self.api.tasks_output(task_id, name)
        self.assertTrue(got["eof"])
        self.assertIn("Reading src", got["text"])
        for raw in ("\x1b", "\x07", "\u202e"):
            self.assertNotIn(raw, got["text"], "no terminal control sequences reach a viewer")
        page = self.api.tasks_output(task_id, name, 0, limit=10)
        self.assertTrue(page["text"].endswith("\n") and not page["eof"], "whole lines, page by page")
        rest = self.api.tasks_output(task_id, name, page["offset"])
        self.assertEqual(page["text"] + rest["text"], got["text"])

    def test_only_listed_outputs_can_be_read(self):
        task_id = self.api.tasks_submit("add a footer", self.ws.ui)["task_id"]
        for bad in ("../task", "../../../etc/passwd", "nope"):
            with self.assertRaises(ApiError) as ctx:
                self.api.tasks_output(task_id, bad)
            self.assertEqual(ctx.exception.kind, "not_found")

    def test_environment_secrets_and_keys_split_over_lines_are_redacted(self):
        log = self.eng.store.live_log("1", "x")
        self.eng.store._secrets = [("MY_TOKEN", "value-from-the-environment")]
        for line in ["a value-from-the-environment b", "x -----BEGIN PRIVATE KEY-----", "AAAA", "BBBB",
                     "-----END PRIVATE KEY----- after", '{"t": "-----BEGIN EC PRIVATE KEY-----\\nQQ\\n-----END EC PRIVATE KEY-----"}']:
            log.write(line)
        with open(log.path, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("value-from-the-environment", text)
        self.assertNotIn("AAAA", text)
        self.assertNotIn("QQ", text)
        self.assertIn(" after", text, "text after the key is kept")


class OpenedProjectsTest(unittest.TestCase):
    """Front ends list the projects the user opened; opening works the same from the app and the CLI."""

    def setUp(self):
        self.ws = Workspace()
        self.s = Script()
        self.eng = make_engine(self.ws, self.s)
        self.api = Api(self.eng, spawn=lambda t, p: self.eng.execute(t))

    def tearDown(self):
        self.ws.cleanup()

    def opened(self, name):
        return next(p for p in self.api.projects_list()["projects"] if p["name"] == name)["opened"]

    def test_opening_hiding_and_working_in_a_project(self):
        self.assertIsNone(self.opened("admin-ui"), "found by a scan: not opened")
        self.assertIsInstance(self.api.projects_open(self.ws.ui)["opened"], float)
        self.assertEqual(self.api.projects_hide("admin-ui")["opened"], False)
        self.api.conversations_send(self.ws.api, "thanks")  # working from a project's folder opens it
        self.assertIsInstance(self.opened("orders-api"), float)
        self.assertIs(self.opened("admin-ui"), False, "a hidden project stays hidden until it is opened again")
        self.api.projects_open(self.ws.ui)
        self.assertIsInstance(self.opened("admin-ui"), float)
        with self.assertRaises(ApiError) as ctx:
            self.api.projects_open(os.path.expanduser("~"))
        self.assertEqual(ctx.exception.kind, "invalid")
        new = os.path.join(self.ws.tmp, "elsewhere", "fresh-project")
        os.makedirs(new)
        self.assertEqual(self.api.projects_open(new)["name"], "fresh-project", "a picked folder becomes a project")
        self.eng.registry.scan()
        self.assertIsInstance(self.opened("fresh-project"), float, "a rescan keeps what was opened")

    def cli(self, *argv, code=0):
        from orchestrator.cli import main
        old = os.environ.get("FOREMAN_HOME")
        os.environ["FOREMAN_HOME"] = self.ws.home
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(main(list(argv)), code, err.getvalue())
        finally:
            if old is None:
                os.environ.pop("FOREMAN_HOME", None)
            else:
                os.environ["FOREMAN_HOME"] = old
        return out.getvalue() + err.getvalue()

    def test_the_cli_offers_what_the_app_does(self):
        conv = self.api.conversations_send(self.ws.ui, "thanks")
        self.assertIn(conv["conversation_id"], self.cli("conversations"))
        self.assertIn("> thanks", self.cli("conversations", conv["conversation_id"]))
        self.assertIn("auto_switch = off", self.cli("config", "auto_switch", "off"))
        self.assertIn("auto-switch = on", self.cli("config", "auto-switch", "on"), "the old names still work")
        self.assertIn("output_style", self.cli("config", "--list"))
        self.assertIn("choose one of", self.cli("config", "output_style", "loud", code=1))
        self.assertIn("default", self.cli("models", "claude", "default"))
        task = self.eng.store.new_task("x", self.ws.ui)
        self.assertIn("is not running", self.cli("stop", task["id"]))
        self.assertIn("no recorded output", self.cli("output", task["id"]))
        self.assertIn("No task", self.cli("stop", "999", code=1))
        self.assertIn("Opened admin-ui", self.cli("open", self.ws.ui, "--no-chat"))
        self.assertIn("off the list", self.cli("projects", "hide", "admin-ui"))
        self.assertIs(self.opened("admin-ui"), False)
        self.assertIn("Totals", self.api.stats()["text"] + "Totals")


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

    def test_a_running_task_says_what_it_is_doing(self):
        task = self.eng.store.new_task("add a footer", self.ws.ui)
        task["status"] = "running"
        self.eng.store.save(task)
        self.eng.store.event(task["id"], "step_start", project="admin-ui", step_kind="implement", provider="claude")
        self.feed.subscribe([task["id"]])  # from now: the phase so far comes from the history

        def phases():
            return [p for m, p in self.feed.poll() if m == "phase"]
        self.assertEqual(phases(), [{"task_id": task["id"], "text": "Implementing in admin-ui", "provider": "claude"}])
        self.assertEqual(phases(), [], "said once")
        self.eng.store.event(task["id"], "activity", text="Read(src/footer.js)", where="admin-ui", provider="claude")
        self.assertEqual([p["text"] for p in phases()], ["Reading footer.js"])
        self.eng.store.event(task["id"], "activity", text="Read(src/footer.js)", where="admin-ui", provider="claude")
        self.assertEqual(phases(), [], "the same line again is not news")
        self.eng.store.event(task["id"], "finished", status="done")
        self.assertEqual(phases(), [], "a finished task has no phase")


class FrontEndsTest(unittest.TestCase):
    """The MCP server and the CLI's --json output go through the same API as the desktop app."""

    def setUp(self):
        self.ws = Workspace()
        self.s = Script()
        self.s.plan, self.s.step = lambda req: change_plan(), edit
        self.eng = make_engine(self.ws, self.s)
        self.started = []

        def spawn(task, plan_only):
            self.started.append((task["id"], plan_only))
            self.eng.plan_only(task) if plan_only else self.eng.execute(task)
        self.mcp = MCPServer(engine_factory=lambda visual=None: self.eng, spawn=spawn)

    def tearDown(self):
        self.ws.cleanup()

    def tool(self, name, **args):
        resp = self.mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
        return resp["result"]["content"][0]["text"], bool(resp["result"].get("isError"))

    def test_mcp_tools_start_runners_through_the_api(self):
        text, err = self.tool("orchestrate", request="add a footer", cwd=self.ws.ui, wait_seconds=5)
        self.assertFalse(err)
        self.assertIn("Footer", text)
        task_id = self.started[0][0]
        self.assertEqual(self.eng.store.events(task_id)[0]["actor"], "mcp")
        self.assertIn("[done] Footer", self.tool("list_tasks")[0])
        self.assertEqual(json.loads(self.tool("show_task", task="footer")[0])["id"], task_id)

        text, _ = self.tool("orchestrate", request="add a footer", cwd=self.ws.ui, dry_run=True, wait_seconds=5)
        self.assertEqual(self.started[-1][1], True, "a dry run plans in a runner too")
        self.assertIn("add a footer", text, "the plan is the report")
        self.assertIn("No task", self.tool("resume_task", task="#999", cwd=self.ws.ui)[0])
        self.assertEqual(self.tool("projects", action="link", src="admin-ui", dst="nowhere")[0], "No project 'nowhere'.")
        text, err = self.tool("orchestrate", request="x", cwd="/no/such/folder")
        self.assertTrue(err, "a bad call is a tool error")

    def test_mcp_free_text_answers(self):
        self.s.plan = lambda req: change_plan(needs_approval=True)
        text, _ = self.tool("orchestrate", request="change refunds", cwd=self.ws.ui, wait_seconds=5)
        task_id = self.started[0][0]
        self.assertEqual(self.eng.store.load(task_id)["status"], "needs_input")
        text, err = self.tool("answer_question", task=task_id, answer="yes", wait_seconds=5)
        self.assertFalse(err)
        self.assertEqual(self.eng.store.load(task_id)["status"], "done")

    def cli(self, *argv):
        from orchestrator.cli import main
        old = os.environ.get("FOREMAN_HOME")
        os.environ["FOREMAN_HOME"] = self.ws.home
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                self.assertEqual(main(list(argv)), 0)
        finally:
            if old is None:
                os.environ.pop("FOREMAN_HOME", None)
            else:
                os.environ["FOREMAN_HOME"] = old
        return json.loads(buf.getvalue())

    def test_cli_json_uses_the_api_shapes(self):
        task_id = Api(self.eng, spawn=lambda t, p: self.eng.execute(t)).tasks_submit("add a footer", self.ws.ui)["task_id"]
        listed = self.cli("tasks", "--json")
        self.assertEqual(listed[0]["id"], task_id)
        self.assertTrue({"rev", "running", "questions", "created_at"} <= set(listed[0]), "a TaskSummary")
        self.assertEqual(self.cli("show", task_id, "--json")["id"], task_id)
        status = self.cli("status", task_id, "--json")
        self.assertEqual((status["task"]["id"], status["task"]["status"]), (task_id, "done"))
        self.assertIn("rev", status["task"])


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
        self.assertEqual(self.rpc("system.hello", {"client": "t", "api_version": "1.0"})["result"]["api_version"], "1.5",
                         "a 1.0 client is served by 1.5 (minor versions only add)")
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
            env = {**os.environ, "FOREMAN_HOME": ws.home, "PYTHONPATH": SRC, "FOREMAN_LOGIN_ENV": "0"}
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
