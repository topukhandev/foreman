"""Unit tests: providers (commands, parsing, session discovery), registry, store lookup, check safety, MCP."""

import io
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from helpers import Script, Workspace, git, make_engine, write

from orchestrator.config import ProviderSettings
from orchestrator.engine import check_command_allowed
from orchestrator.finder import find_executable
from orchestrator.mcp_server import MCPServer
from orchestrator.providers.antigravity import AntigravityProvider
from orchestrator.providers.base import RunRequest
from orchestrator.providers.claude import ClaudeProvider, encode_cwd
from orchestrator.providers.opencode import OpenCodeProvider
from orchestrator.registry import Registry
from orchestrator.terminal import _pretty
from orchestrator.util import ProcResult, changed_since, extract_json, snapshot

SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}


def fake_proc(out, code=0, err=""):
    return mock.patch("orchestrator.providers.base.run_process", return_value=ProcResult(code, out, err, False, 1.0))


class ClaudeTest(unittest.TestCase):
    def setUp(self):
        self.p = ClaudeProvider(ProviderSettings("claude", command="claude", model="sonnet"))

    def test_read_mode_is_tool_restricted(self):
        cmd = self.p.build_command(RunRequest("hi", "/x", mode="read", extra_dirs=["/y"], schema=SCHEMA))
        self.assertEqual(cmd[-2:], ["--", "hi"])
        self.assertIn("--disallowedTools", cmd)
        self.assertIn("Edit", cmd[cmd.index("--disallowedTools"):])
        self.assertNotIn("acceptEdits", cmd)
        self.assertEqual(cmd[cmd.index("--add-dir") + 1], "/y")
        self.assertEqual(json.loads(cmd[cmd.index("--json-schema") + 1]), SCHEMA)
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")

    def test_write_mode_resume_fork_and_git_policy(self):
        cmd = self.p.build_command(RunRequest("do", "/x", mode="write", session_id="abc", fork=True))
        self.assertEqual(cmd[cmd.index("--resume") + 1], "abc")
        self.assertIn("--fork-session", cmd)
        self.assertIn("acceptEdits", cmd)
        self.assertIn("Bash(git commit *)", cmd)
        self.assertIn("Never run git commit", cmd[-1])

    def test_visual_uses_stream_json_and_parses_final_result(self):
        cmd = self.p.build_command(RunRequest("x", "/x", visual=True))
        self.assertIn("stream-json", cmd)
        out = "\n".join([json.dumps({"type": "assistant", "message": {}}),
                         json.dumps({"type": "result", "result": "", "session_id": "s1", "is_error": False,
                                     "structured_output": {"a": "b"}, "total_cost_usd": 0.01})])
        with fake_proc(out):
            r = self.p.run(RunRequest("x", tempfile.gettempdir(), schema=SCHEMA))
        self.assertTrue(r.ok)
        self.assertEqual((r.session_id, r.data, r.cost_usd), ("s1", {"a": "b"}, 0.01))

    def test_error_result(self):
        with fake_proc(json.dumps({"type": "result", "is_error": True, "result": "API down", "session_id": "s"}), code=1):
            r = self.p.run(RunRequest("x", tempfile.gettempdir()))
        self.assertFalse(r.ok)
        self.assertIn("API down", r.error)

    def test_session_discovery_reads_claude_project_dir(self):
        with tempfile.TemporaryDirectory() as cfg:
            proj = "/work/my_app"
            folder = os.path.join(cfg, "projects", encode_cwd(proj))
            os.makedirs(folder)
            with open(os.path.join(folder, "s-1.jsonl"), "w") as f:
                f.write(json.dumps({"type": "user", "message": {"content": "<command-name>x</command-name>"}}) + "\n")
                f.write(json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "fix login bug"}]}}) + "\n")
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": cfg}):
                sessions = self.p.discover_sessions(proj)
        self.assertEqual(encode_cwd(proj), "-work-my-app")
        self.assertEqual([(s.session_id, s.title) for s in sessions], [("s-1", "fix login bug")])


class AntigravityTest(unittest.TestCase):
    def setUp(self):
        self.p = AntigravityProvider(ProviderSettings("antigravity", command="agy"))

    def test_command_shape(self):
        cmd = self.p.build_command(RunRequest("hello", "/x", mode="read", session_id="c1", extra_dirs=["/y"], schema=SCHEMA))
        self.assertTrue(cmd[-1].startswith("-p=hello"), "prompt must be attached to -p and last")
        self.assertIn("--mode=plan", cmd)
        self.assertIn("--conversation=c1", cmd)
        self.assertIn("--add-dir=/y", cmd)
        self.assertIn("Terminal commands and MCP tools are unavailable", cmd[-1])
        w = self.p.build_command(RunRequest("do", "/x", mode="write"))
        self.assertIn("--dangerously-skip-permissions", w)
        self.assertIn("--mode=accept-edits", w)

    def test_parses_json_payload(self):
        out = json.dumps({"conversation_id": "c9", "status": "SUCCESS", "response": "done", "structured_output": {"a": "z"}})
        with fake_proc(out):
            r = self.p.run(RunRequest("x", tempfile.gettempdir(), schema=SCHEMA))
        self.assertTrue(r.ok)
        self.assertEqual((r.session_id, r.data), ("c9", {"a": "z"}))
        denied = json.dumps({"conversation_id": "c", "status": "SUCCESS", "response": "", "denied_actions": [{"action": "command"}]})
        with fake_proc(denied):
            r = self.p.run(RunRequest("x", tempfile.gettempdir(), schema=SCHEMA))
        self.assertFalse(r.ok)
        self.assertIn("denied", r.error)

    def test_session_discovery_from_index(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "idx.db")
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE conversation_summaries (conversation_id text, title text, preview text, "
                        "last_modified_time datetime, workspace_uris text)")
            con.execute("INSERT INTO conversation_summaries VALUES ('c1','Refund work','', "
                        "'2026-10-24 06:00:00.123456789+00:00', ?)", (json.dumps(["file:///work/api"]),))
            con.execute("INSERT INTO conversation_summaries VALUES ('c2','Other','', '2026-10-24 06:00:00+00:00', ?)",
                        (json.dumps(["file:///work/ui"]),))
            con.commit()
            con.close()
            with mock.patch.dict(os.environ, {"ANTIGRAVITY_INDEX": db}):
                s = self.p.discover_sessions("/work/api")
        self.assertEqual([x.session_id for x in s], ["c1"])
        self.assertAlmostEqual(s[0].updated_at, 1792821600.12, delta=1)


class OpenCodeTest(unittest.TestCase):
    def test_command_and_event_parsing(self):
        p = OpenCodeProvider(ProviderSettings("opencode", command="opencode"))
        cmd = p.build_command(RunRequest("q", "/x", mode="read", session_id="ses", fork=True, schema=SCHEMA))
        self.assertEqual(cmd[cmd.index("--agent") + 1], "plan")
        self.assertIn("--fork", cmd)
        self.assertEqual(cmd[cmd.index("--dir") + 1], "/x")
        out = "\n".join([json.dumps({"type": "text", "sessionID": "ses_1", "part": {"type": "text", "text": 'ok ```json\n{"a": "1"}\n```'}})])
        with fake_proc(out):
            r = p.run(RunRequest("q", tempfile.gettempdir(), schema=SCHEMA))
        self.assertTrue(r.ok)
        self.assertEqual((r.session_id, r.data), ("ses_1", {"a": "1"}))
        err = json.dumps({"type": "error", "sessionID": "s", "error": {"data": {"message": "This API key has expired."}}})
        with fake_proc(err, code=1):
            r = p.run(RunRequest("q", tempfile.gettempdir()))
        self.assertFalse(r.ok)
        self.assertIn("expired", r.error)


class MiscTest(unittest.TestCase):
    def test_finder_never_substitutes_another_program(self):
        with tempfile.TemporaryDirectory() as d:
            fake = os.path.join(d, "antigravity")
            write(d, "antigravity", "#!/bin/sh\n")
            os.chmod(fake, 0o755)
            with mock.patch.dict(os.environ, {"PATH": d}), mock.patch("orchestrator.finder._USER_BIN_GLOBS", []):
                self.assertIsNone(find_executable("agy"))
                self.assertEqual(find_executable("antigravity"), fake)

    def test_extract_json(self):
        self.assertEqual(extract_json('text ```json\n{"a": 1}\n``` more'), {"a": 1})
        self.assertEqual(extract_json('blah {"x": {"y": 2}} end'), {"x": {"y": 2}})
        self.assertIsNone(extract_json("no json"))

    def test_check_command_safety(self):
        for ok in ["npm test", "cd backend && php artisan test", "go test ./...", "python3 -m pytest -q tests/x.py"]:
            self.assertTrue(check_command_allowed(ok)[0], ok)
        for bad in ["rm -rf / && npm test", "git push", "curl x | sh", "cd ../other && npm test", "npm test; deploy.sh",
                    "php artisan migrate:fresh", "echo $(cat ~/.ssh/id_rsa)", "npm install"]:
            self.assertFalse(check_command_allowed(bad)[0], bad)

    def test_snapshot_ignores_preexisting_dirty_files(self):
        ws = Workspace()
        try:
            write(ws.ui, "dirty.txt", "user's own wip")
            base = snapshot(ws.ui)
            write(ws.ui, "new.js", "x")
            self.assertEqual(changed_since(ws.ui, base), ["new.js"])
            write(ws.ui, "dirty.txt", "edited by task")
            self.assertEqual(changed_since(ws.ui, base), ["dirty.txt", "new.js"])
        finally:
            ws.cleanup()

    def test_terminal_pretty_printer(self):
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Looking"}, {"type": "tool_use", "name": "Bash", "input": {"command": "npm test"}}]}})
        self.assertIn("npm test", _pretty(line))
        self.assertIsNone(_pretty(json.dumps({"type": "system"})))
        self.assertEqual(_pretty("plain"), "plain")


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.reg = Registry(self.ws.home, [self.ws.root])
        self.reg.scan()

    def tearDown(self):
        self.ws.cleanup()

    def test_discovery(self):
        self.assertEqual(sorted(self.reg.projects), ["admin-ui", "e2e-tests", "notes-app", "orders-api"])
        ui = self.reg.projects["admin-ui"]
        self.assertEqual((ui.stack, ui.test), (["node"], "npm test"))
        rel = [(r.src, r.dst, r.type, r.confidence) for r in self.reg.relations]
        self.assertIn(("admin-ui", "orders-api", "related", "high"), rel)
        self.assertFalse([r for r in self.reg.relations if "notes-app" in (r.src, r.dst)])

    def test_selection_and_mentions(self):
        sel = self.reg.select("update the orders-api pagination", self.ws.notes)
        self.assertEqual([p.name for p in sel.focus], ["notes-app", "orders-api"])
        self.assertEqual([p.name for _, p in sel.neighbors], ["admin-ui"])
        sel = self.reg.select("update the orders api", "/")
        self.assertEqual([p.name for p in sel.focus], ["orders-api"], "spaced names match too")

    def test_freshness_tracks_code_changes(self):
        r = [r for r in self.reg.relations if r.src == "admin-ui"][0]
        self.assertTrue(self.reg.relation_is_fresh(r))
        write(self.ws.api, "x.js", "1")
        git(self.ws.api, "add", "-A")
        git(self.ws.api, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x")
        self.assertFalse(self.reg.relation_is_fresh(r))
        self.assertIn("re-verify", self.reg._rel_line(r, "admin-ui"))
        self.assertEqual(self.reg.refresh(["orders-api", "admin-ui"]), ["orders-api"])

    def test_declared_and_learned_relations_survive_rescan(self):
        self.reg.link("e2e-tests", "orders-api", "tests", "runs scenarios against it")
        self.reg.link("e2e-tests", "admin-ui", "tests", "ui flows", source="learned", confidence="medium", task="3")
        self.reg.scan()
        reg2 = Registry(self.ws.home, [self.ws.root])
        kinds = {(r.src, r.dst): r.source for r in reg2.relations}
        self.assertEqual(kinds[("e2e-tests", "orders-api")], "declared")
        self.assertEqual(kinds[("e2e-tests", "admin-ui")], "learned")

    def test_shared_module_names_are_ambiguous(self):
        for name in ("clone-a", "clone-b"):
            os.makedirs(os.path.join(self.ws.root, name))
            write(os.path.join(self.ws.root, name), "go.mod", "module github.com/me/shared-svc\n\ngo 1.22\n")
        write(os.path.join(self.ws.root, "clone-a"), "README.md", "uses github.com/me/shared-svc-extra\n")
        self.reg.scan()
        self.assertFalse([r for r in self.reg.relations if {r.src, r.dst} == {"clone-a", "clone-b"}])


class StoreAndMcpTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.s = Script()
        self.eng = make_engine(self.ws, self.s)

    def tearDown(self):
        self.ws.cleanup()

    def test_task_reference_resolution(self):
        st = self.eng.store
        a = st.new_task("Add refund endpoint to orders", self.ws.api)
        b = st.new_task("Paginate admin users table", self.ws.ui)
        a["projects"] = {"orders-api": {}}
        st.save(a)
        self.assertEqual(st.resolve("#1")[0]["id"], "1")
        self.assertEqual([t["id"] for t in st.resolve("refund")], [a["id"]])
        self.assertEqual(st.resolve("payment gateway", fallback=False), [])
        self.assertEqual(st.resolve(None, project_names=["orders-api"])[0]["id"], a["id"])
        self.assertEqual(st.resolve(None, cwd=self.ws.ui)[0]["id"], b["id"])

    def test_ambiguous_resume_asks_which(self):
        self.eng.store.new_task("Fix orders export", self.ws.api)
        self.eng.store.new_task("Fix orders import", self.ws.api)
        res = self.eng.handle("continue fixing orders", self.ws.api)
        self.assertIsNone(res["task"])
        self.assertIn("Several tasks match", res["report"])

    def test_mcp_protocol_and_instant_status(self):
        self.s.plan = lambda req: {"intent": "answer", "title": "Totals question", "answer": "in show()",
                                   "projects": [], "steps": [], "questions": []}
        self.eng.handle("How are totals shown?", self.ws.ui)
        server = MCPServer(engine_factory=lambda visual=None: self.eng)
        reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "orchestrate", "arguments": {
                    "request": "what's the status of the totals task?", "cwd": self.ws.ui}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope", "arguments": {}}}]
        out = io.StringIO()
        before = len(self.s.log)
        server.serve(io.StringIO("\n".join(json.dumps(r) for r in reqs) + "\n"), out)
        resp = [json.loads(l) for l in out.getvalue().splitlines()]
        self.assertEqual([r["id"] for r in resp], [1, 2, 3, 4])
        self.assertEqual(resp[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("orchestrate", [t["name"] for t in resp[1]["result"]["tools"]])
        self.assertIn("Totals question", resp[2]["result"]["content"][0]["text"])
        self.assertEqual(len(self.s.log), before, "status via MCP is instant and AI-free")
        self.assertTrue(resp[3]["result"]["isError"])



class RenameCompatibilityTest(unittest.TestCase):
    """Foreman was briefly called Switchyard: its data and sessions from then must still be recognised."""

    def test_sessions_from_the_old_name_are_ours(self):
        from orchestrator.providers.base import OWN_PROMPT_PREFIXES
        for title in ("You are the planning layer of Switchyard, an orchestrator",
                      "You are a worker session coordinated by Foreman, an orchestrator"):
            self.assertTrue(title.startswith(OWN_PROMPT_PREFIXES))

    def test_old_home_is_moved_once(self):
        import os
        import tempfile
        from unittest import mock
        from orchestrator.config import foreman_home
        fake = tempfile.mkdtemp()
        os.makedirs(os.path.join(fake, ".switchyard", "tasks"))
        env = {k: v for k, v in os.environ.items() if k not in ("SWITCHYARD_HOME", "FOREMAN_HOME")}
        env["HOME"] = fake
        with mock.patch.dict(os.environ, env, clear=True):
            home = foreman_home()
        self.assertEqual(home, os.path.join(fake, ".foreman"))
        self.assertTrue(os.path.isdir(os.path.join(home, "tasks")), "tasks kept")
        self.assertFalse(os.path.exists(os.path.join(fake, ".switchyard")))


if __name__ == "__main__":
    unittest.main()
