"""CLI interface for Orchestrator (Foreman / Quorum)."""

import argparse
import os
import sys
from orchestrator.config import load_config, init_default_config_file
from orchestrator.doctor import SetupDoctor
from orchestrator.hub import Hub
from orchestrator.engine import OrchestratorEngine
from orchestrator.progress import ProgressTracker
from orchestrator.wizard import run_setup_wizard
from orchestrator.mcp_server import MCPServer
from orchestrator.packs import PACKS
from orchestrator.repl import InteractiveShell


def cmd_init(args: argparse.Namespace) -> int:
    target_dir = os.path.abspath(args.path or ".")
    os.makedirs(target_dir, exist_ok=True)
    hub = Hub(target_dir)
    config_path = os.path.join(target_dir, "providers.yaml")
    init_default_config_file(config_path)

    print(f"[OK] Initialized Orchestrator project at: {target_dir}")
    print(f"     Hub created at: {hub.hub_dir}")
    print(f"     Config file: {config_path}")
    print("\nNext step: Run 'orchestrator' to launch interactive shell, or 'orchestrator doctor'.")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    target_dir = os.path.abspath(args.path or ".")
    run_setup_wizard(target_dir, preset=args.preset, non_interactive=args.non_interactive)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    target_dir = os.path.abspath(args.path or ".")
    config_path = os.path.join(target_dir, "providers.yaml")
    cfg = load_config(config_path) if os.path.exists(config_path) else None
    report = SetupDoctor.render_report(cfg)
    print(report)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    target_dir = os.path.abspath(args.path or ".")
    hub = Hub(target_dir)
    state_content = hub.read_state()
    if not state_content:
        print("[!] No active state found. Has the orchestrator been initialized here?")
        return 1
    print(state_content)
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    target_dir = os.path.abspath(args.project or ".")
    server = MCPServer(default_project_dir=target_dir)
    server.serve_forever()
    return 0


def cmd_shell(args: argparse.Namespace) -> int:
    target_dir = os.path.abspath(getattr(args, "path", ".") or ".")
    shell = InteractiveShell(project_dir=target_dir)
    shell.run()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    target_dir = os.path.abspath(args.project or ".")
    os.makedirs(target_dir, exist_ok=True)
    config_path = os.path.join(target_dir, "providers.yaml")
    cfg = load_config(config_path)

    if args.mock:
        for p in cfg.providers.values():
            p.enabled = False
        if "mock" in cfg.providers:
            cfg.providers["mock"].enabled = True
            cfg.providers["mock"].verified = True
        else:
            from orchestrator.config import ProviderConfig
            cfg.providers["mock"] = ProviderConfig(
                name="mock",
                enabled=True,
                tier="free",
                command="mock",
                roles=["planner", "critic", "coder", "researcher", "reviewer"],
                verified=True
            )

    goal = args.goal or "Validate and prepare a launch strategy for an AI Developer Tool"
    pack_name = args.pack or "developer"

    def print_progress(text: str):
        print("\n" + text)

    print(f"Starting Orchestrator in: {target_dir}")
    print(f"Goal: {goal}\n")

    engine = OrchestratorEngine(
        project_dir=target_dir,
        config=cfg,
        status_callback=print_progress
    )

    result = engine.run_pack(pack_name, goal)

    print("\n" + "=" * 60)
    print("                ORCHESTRATOR RUN COMPLETE")
    print("=" * 60)
    print(result.summary_report)
    if result.blockers:
        print("\nBlockers / Warnings:")
        for b in result.blockers:
            print(f"  - {b}")
    print("=" * 60)

    return 0 if result.success else 1


def main() -> None:
    # If launched with zero arguments, default to interactive shell!
    if len(sys.argv) == 1:
        shell = InteractiveShell(project_dir=".")
        shell.run()
        sys.exit(0)

    parser = argparse.ArgumentParser(
        prog="orchestrator",
        description="Local AI Orchestrator (Foreman / Quorum)"
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=False)

    # shell (interactive console)
    p_shell = subparsers.add_parser("shell", help="Launch interactive front-desk shell")
    p_shell.add_argument("path", nargs="?", default=".", help="Directory to open")
    p_shell.set_defaults(func=cmd_shell)

    # init
    p_init = subparsers.add_parser("init", help="Initialize an orchestrator workspace")
    p_init.add_argument("path", nargs="?", default=".", help="Directory to initialize")
    p_init.set_defaults(func=cmd_init)

    # setup
    p_setup = subparsers.add_parser("setup", help="Interactive first-run configuration wizard")
    p_setup.add_argument("path", nargs="?", default=".", help="Directory to configure")
    p_setup.add_argument("--preset", choices=["1", "2", "3", "4", "5", "6", "auto", "antigravity_only", "claude_only", "dual_model", "free_only", "mock"], help="Preset configuration")
    p_setup.add_argument("--non-interactive", action="store_true", help="Apply preset without prompting")
    p_setup.set_defaults(func=cmd_setup)

    # doctor
    p_doctor = subparsers.add_parser("doctor", help="Diagnose local AI CLI tools and dependencies")
    p_doctor.add_argument("path", nargs="?", default=".", help="Project directory")
    p_doctor.set_defaults(func=cmd_doctor)

    # status
    p_status = subparsers.add_parser("status", help="Show current state.md and progress")
    p_status.add_argument("path", nargs="?", default=".", help="Project directory")
    p_status.set_defaults(func=cmd_status)

    # mcp
    p_mcp = subparsers.add_parser("mcp", help="Start local Model Context Protocol (MCP) server over stdio")
    p_mcp.add_argument("--project", default=".", help="Project directory")
    p_mcp.set_defaults(func=cmd_mcp)

    # run
    p_run = subparsers.add_parser("run", help="Run a multi-agent workflow on a goal")
    p_run.add_argument("goal", nargs="?", default="", help="Goal or user prompt")
    p_run.add_argument("--pack", choices=list(PACKS.keys()), default="developer", help="Role pack to execute (developer, business_idea, qa)")
    p_run.add_argument("--project", default=".", help="Project directory")
    p_run.add_argument("--mock", action="store_true", help="Run with mock/simulation workers")
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    if not getattr(args, "func", None):
        # Default to shell if unknown command or empty
        shell = InteractiveShell(project_dir=".")
        shell.run()
        sys.exit(0)

    code = args.func(args)
    sys.exit(code)


if __name__ == "__main__":
    main()
