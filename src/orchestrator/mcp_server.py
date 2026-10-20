"""Local MCP Server (JSON-RPC over stdio) for GUI client integration (e.g. Antigravity desktop)."""

import json
import os
import sys
from typing import Any, Dict, List, Optional
from orchestrator.config import load_config
from orchestrator.hub import Hub
from orchestrator.doctor import SetupDoctor
from orchestrator.budget import BudgetGuard
from orchestrator.engine import OrchestratorEngine
from orchestrator.packs import PACKS


class MCPServer:
    """Implements Model Context Protocol server over stdio."""

    SERVER_NAME = "orchestrator-mcp"
    SERVER_VERSION = "0.1.0"
    PROTOCOL_VERSION = "2024-11-05"

    TOOLS = [
        {
            "name": "get_orchestrator_state",
            "description": "Get current one-page orchestrator state from hub/state.md (active tasks, blockers, goal).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_dir": {
                        "type": "string",
                        "description": "Path to the project directory containing hub/."
                    }
                },
                "required": ["project_dir"]
            }
        },
        {
            "name": "list_tasks",
            "description": "List all tasks and their states (pending, running, reviewing, done, failed) from hub/status/.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_dir": {
                        "type": "string",
                        "description": "Path to the project directory."
                    }
                },
                "required": ["project_dir"]
            }
        },
        {
            "name": "get_task_details",
            "description": "Read task markdown specification, instructions, worker results, and reviewer notes.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_dir": {
                        "type": "string",
                        "description": "Path to the project directory."
                    },
                    "task_id": {
                        "type": "string",
                        "description": "The task ID (e.g. '1', '2')."
                    }
                },
                "required": ["project_dir", "task_id"]
            }
        },
        {
            "name": "get_budget_status",
            "description": "Check current daily run quotas, active worker slots, task bounces, and budget warnings.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_dir": {
                        "type": "string",
                        "description": "Path to the project directory."
                    }
                },
                "required": ["project_dir"]
            }
        },
        {
            "name": "doctor_diagnostics",
            "description": "Diagnose local AI CLI worker tools and dependencies with OS fix commands.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_dir": {
                        "type": "string",
                        "description": "Optional path to project directory to check providers.yaml."
                    }
                }
            }
        },
        {
            "name": "run_orchestration_goal",
            "description": "Run an end-to-end multi-agent orchestration loop on a goal using a specified role pack.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_dir": {
                        "type": "string",
                        "description": "Path to the project directory."
                    },
                    "goal": {
                        "type": "string",
                        "description": "The goal or prompt to achieve."
                    },
                    "pack": {
                        "type": "string",
                        "description": "Role pack to use (business_idea, developer, qa). Default is developer.",
                        "enum": ["business_idea", "developer", "qa"]
                    },
                    "use_mock": {
                        "type": "boolean",
                        "description": "Whether to run with offline simulation workers."
                    }
                },
                "required": ["project_dir", "goal"]
            }
        }
    ]

    def __init__(self, default_project_dir: str = "."):
        self.default_project_dir = os.path.abspath(default_project_dir)

    def handle_call(self, name: str, args: Dict[str, Any]) -> str:
        """Dispatch tool calls and return text output."""
        project_dir = os.path.abspath(args.get("project_dir", self.default_project_dir))

        if name == "get_orchestrator_state":
            hub = Hub(project_dir)
            state = hub.read_state()
            return state or "No active state found."

        elif name == "list_tasks":
            hub = Hub(project_dir)
            statuses = hub.get_all_task_statuses()
            if not statuses:
                return "No tasks found in hub/status/."
            lines = [f"Found {len(statuses)} task(s):"]
            for s in statuses:
                lines.append(f"- Task #{s.task_id}: {s.title} [{s.role}] -> {s.state.upper()} ({s.one_line_summary})")
            return "\n".join(lines)

        elif name == "get_task_details":
            task_id = str(args.get("task_id", ""))
            hub = Hub(project_dir)
            spec = hub.read_task_spec(task_id)
            return spec or f"Task #{task_id} not found."

        elif name == "get_budget_status":
            cfg_path = os.path.join(project_dir, "providers.yaml")
            cfg = load_config(cfg_path)
            ledger = os.path.join(project_dir, "hub", ".budget_ledger.json")
            guard = BudgetGuard(cfg.budget, cfg.providers, ledger)
            stat = guard.get_status()
            return json.dumps({
                "daily_runs": stat.provider_runs_today,
                "active_workers": stat.active_workers,
                "active_tasks": stat.active_tasks,
                "bounces": stat.bounces_by_task,
                "warnings": stat.warnings
            }, indent=2)

        elif name == "doctor_diagnostics":
            cfg_path = os.path.join(project_dir, "providers.yaml")
            cfg = load_config(cfg_path) if os.path.exists(cfg_path) else None
            return SetupDoctor.render_report(cfg)

        elif name == "run_orchestration_goal":
            goal = args.get("goal", "")
            pack_name = args.get("pack", "developer")
            use_mock = bool(args.get("use_mock", False))

            cfg_path = os.path.join(project_dir, "providers.yaml")
            cfg = load_config(cfg_path)

            if use_mock:
                for p in cfg.providers.values():
                    p.enabled = False
                if "mock" in cfg.providers:
                    cfg.providers["mock"].enabled = True
                    cfg.providers["mock"].verified = True

            engine = OrchestratorEngine(project_dir, cfg)
            result = engine.run_pack(pack_name, goal)
            return (
                f"Status: {'SUCCESS' if result.success else 'FAILED'}\n"
                f"Elapsed: {int(result.elapsed_seconds)}s\n"
                f"Completed: {result.tasks_completed} tasks\n"
                f"Report:\n{result.summary_report}"
            )

        else:
            return f"Error: Unknown tool '{name}'"

    def process_message(self, line: str) -> Optional[Dict[str, Any]]:
        """Parse JSON-RPC line and return response dictionary."""
        if not line.strip():
            return None
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"}
            }

        msg_id = req.get("id")
        method = req.get("method")
        params = req.get("params", {})

        # Notifications (no id)
        if msg_id is None:
            return None

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": self.PROTOCOL_VERSION,
                    "capabilities": {
                        "tools": {}
                    },
                    "serverInfo": {
                        "name": self.SERVER_NAME,
                        "version": self.SERVER_VERSION
                    }
                }
            }

        elif method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "tools": self.TOOLS
                }
            }

        elif method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments", {})
            try:
                output = self.handle_call(name, args)
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [
                            {"type": "text", "text": output}
                        ]
                    }
                }
            except Exception as e:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "isError": True,
                        "content": [
                            {"type": "text", "text": f"Tool execution failed: {str(e)}"}
                        ]
                    }
                }

        else:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Method '{method}' not found"}
            }

    def serve_forever(self) -> None:
        """Run stdio loop until EOF."""
        for line in sys.stdin:
            resp = self.process_message(line)
            if resp:
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
