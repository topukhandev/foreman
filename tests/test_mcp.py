import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))
"""Tests for Local MCP Server."""

import json
import os
import tempfile
import unittest
from orchestrator.mcp_server import MCPServer
from orchestrator.hub import Hub


class TestMCPServer(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.hub = Hub(self.temp_dir.name)
        self.server = MCPServer(default_project_dir=self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_mcp_initialize(self):
        req = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"}
        })
        resp = self.server.process_message(req)
        self.assertIsNotNone(resp)
        self.assertEqual(resp["id"], 1)
        self.assertEqual(resp["result"]["serverInfo"]["name"], "orchestrator-mcp")

    def test_mcp_tools_list(self):
        req = json.dumps({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {}
        })
        resp = self.server.process_message(req)
        self.assertIsNotNone(resp)
        tool_names = [t["name"] for t in resp["result"]["tools"]]
        self.assertIn("get_orchestrator_state", tool_names)
        self.assertIn("list_tasks", tool_names)
        self.assertIn("run_orchestration_goal", tool_names)

    def test_mcp_tool_call_state(self):
        req = json.dumps({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "get_orchestrator_state",
                "arguments": {"project_dir": self.temp_dir.name}
            }
        })
        resp = self.server.process_message(req)
        self.assertIsNotNone(resp)
        self.assertEqual(resp["id"], 3)
        text_content = resp["result"]["content"][0]["text"]
        self.assertIn("Project State", text_content)


if __name__ == "__main__":
    unittest.main()
