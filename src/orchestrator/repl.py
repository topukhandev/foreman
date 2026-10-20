"""Interactive REPL shell with slash commands, live dropdown suggestions, and sub-menu selectors."""

import os
import readline
import sys
import time
from typing import Dict, List, Optional
import yaml
from orchestrator.config import load_config, OrchestratorConfig
from orchestrator.hub import Hub
from orchestrator.doctor import SetupDoctor
from orchestrator.links import WorkspaceLinks
from orchestrator.engine import OrchestratorEngine
from orchestrator.packs import PACKS


SLASH_COMMANDS = [
    ("/provider", "Select or switch default AI worker (Antigravity, Claude, OpenCode)"),
    ("/status",   "View single-page project state (state.md) & active workers"),
    ("/tasks",    "Inspect recent tasks, specs, and reviewer verdicts"),
    ("/link",     "Connect another directory, repository, or microservice"),
    ("/pack",     "Select default workflow pack (developer, business_idea, qa)"),
    ("/doctor",   "Check installed AI tools & system health diagnostics"),
    ("/clear",    "Clear the terminal screen"),
    ("/help",     "Display interactive command guide"),
    ("/exit",     "Exit orchestrator session")
]


class SlashCompleter:
    """Readline completer for slash commands."""

    def __init__(self, commands: List[tuple]):
        self.commands = [cmd for cmd, _ in commands]

    def complete(self, text: str, state: int) -> Optional[str]:
        matches = [c for c in self.commands if c.startswith(text)]
        if state < len(matches):
            return matches[state]
        return None


class InteractiveShell:
    """Claude Code / Antigravity style interactive terminal console."""

    def __init__(self, project_dir: str = "."):
        self.project_dir = os.path.abspath(project_dir)
        self.hub = Hub(self.project_dir)
        self.links = WorkspaceLinks(self.project_dir)
        self.config_path = os.path.join(self.project_dir, "providers.yaml")
        self.config = load_config(self.config_path)
        self.active_pack = "developer"

        # Setup readline
        self.completer = SlashCompleter(SLASH_COMMANDS)
        readline.set_completer(self.completer.complete)
        readline.parse_and_bind("tab: complete")
        readline.set_completer_delims(" \t\n")

    def _get_active_provider_name(self) -> str:
        """Find currently active primary provider."""
        for name, p in self.config.providers.items():
            if p.enabled and p.verified:
                return name
        return "mock"

    def render_banner(self) -> None:
        """Render welcoming interactive session banner."""
        active_p = self._get_active_provider_name()
        diag = SetupDoctor.check_system(self.config)
        available_tools = [
            t_name for t_name, t_info in diag["tools"].items() if t_info["installed"]
        ]
        services = self.links.list_links()
        services_str = f"{len(services)} linked" if services else "None linked"

        print("========================================================================")
        print(" 🎛️  FOREMAN ORCHESTRATOR — Front-Desk Session")
        print(f" Directory  : {self.project_dir}")
        print(f" Primary AI : {active_p} (Pack: {self.active_pack})")
        print(f" Tools Found: {', '.join(available_tools) or 'None'}")
        print(f" Workspaces : {services_str}")
        print("------------------------------------------------------------------------")
        print(" Type '/' to see slash commands, or enter your goal naturally below.")
        print(" Type /help for usage guide, /exit to quit.")
        print("========================================================================")

    def print_slash_menu(self, filter_prefix: str = "") -> None:
        """Render dropdown menu of available slash commands."""
        matches = [
            (cmd, desc) for cmd, desc in SLASH_COMMANDS if cmd.startswith(filter_prefix)
        ]
        if not matches:
            return

        print("\n  ╭─ Available Commands (Press Tab to complete) ────────────────────────╮")
        for cmd, desc in matches:
            print(f"  │  {cmd:<12} {desc:<55} │")
        print("  ╰─────────────────────────────────────────────────────────────────────╯")

    def handle_provider_menu(self) -> None:
        """Interactive provider selector menu."""
        diag = SetupDoctor.check_system(self.config)
        tools = diag["tools"]

        has_agy = tools.get("antigravity_cli", {}).get("installed", False)
        has_claude = tools.get("claude_code", {}).get("installed", False)
        has_opencode = tools.get("opencode", {}).get("installed", False)

        curr = self._get_active_provider_name()

        print("\n? Select Default AI Provider for this project:")
        print(f"  [1] Antigravity CLI (agy)       {'[INSTALLED]' if has_agy else '[NOT FOUND]'} {'[ACTIVE]' if curr == 'antigravity_cli' else ''}")
        print(f"  [2] Claude Code (claude)        {'[INSTALLED]' if has_claude else '[NOT FOUND]'} {'[ACTIVE]' if curr == 'claude_code' else ''}")
        print(f"  [3] OpenCode (opencode)         {'[INSTALLED]' if has_opencode else '[NOT FOUND]'} {'[ACTIVE]' if curr == 'opencode' else ''}")
        print(f"  [4] Dual-Model (Antigravity builds, Claude reviews) {'[READY]' if (has_agy and has_claude) else '[NEED BOTH]'}")
        print(f"  [5] Simulation / Mock Mode (Zero tokens)")
        print("  [c] Cancel")

        try:
            choice = input("\nEnter choice (1-5 or c): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("")
            return

        from orchestrator.wizard import build_config_for_preset
        preset_map = {"1": "2", "2": "3", "3": "5", "4": "4", "5": "6"}
        if choice in preset_map:
            new_cfg = build_config_for_preset(preset_map[choice], tools)
            with open(self.config_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(new_cfg, f, sort_keys=False)
            self.config = load_config(self.config_path)
            print(f"[OK] Switched active provider to: {self._get_active_provider_name()}")
        elif choice == "c":
            print("Cancelled.")
        else:
            print("Invalid selection.")

    def handle_link_menu(self) -> None:
        """Interactive link menu to connect another directory/service."""
        services = self.links.list_links()
        if services:
            print("\nCurrently Linked Services:")
            for s in services:
                print(f"  - {s.name}: {s.path} ({s.description or 'No desc'})")

        print("\n? Connect an external folder/service:")
        print("  [1] Add new linked folder")
        print("  [2] Remove existing link")
        print("  [c] Cancel")

        try:
            action = input("\nSelect option (1/2/c): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("")
            return

        if action == "1":
            try:
                target_path = input("Enter path to directory (e.g. ~/Desktop/api): ").strip()
                if not target_path:
                    return
                name = input("Enter a service name (e.g. backend, ui, docs) [default: auto]: ").strip()
                desc = input("Brief description of this service: ").strip()
                ok = self.links.add_link(name, target_path, desc)
                if ok:
                    print(f"[OK] Successfully linked service '{name}' at: {os.path.abspath(os.path.expanduser(target_path))}")
                else:
                    print(f"[!] Error: Directory not found: {target_path}")
            except (EOFError, KeyboardInterrupt):
                print("")
        elif action == "2":
            if not services:
                print("No services currently linked.")
                return
            try:
                del_name = input("Enter service name to remove: ").strip()
                if self.links.remove_link(del_name):
                    print(f"[OK] Removed link: {del_name}")
                else:
                    print(f"[!] Link not found: {del_name}")
            except (EOFError, KeyboardInterrupt):
                print("")

    def handle_tasks_menu(self) -> None:
        """Inspect task status and contents interactively."""
        statuses = self.hub.get_all_task_statuses()
        if not statuses:
            print("\n[!] No tasks found in this project workspace yet.")
            return

        print(f"\nTask History ({len(statuses)} tasks):")
        for s in statuses:
            print(f"  Task #{s.task_id}: {s.title} [{s.role}] -> {s.state.upper()} ({s.one_line_summary})")

        try:
            pick = input("\nEnter Task ID to view full specification & results (or Enter to back): ").strip()
            if pick:
                spec = self.hub.read_task_spec(pick)
                if spec:
                    print("\n" + "=" * 60)
                    print(spec)
                    print("=" * 60)
                else:
                    print(f"Task #{pick} not found.")
        except (EOFError, KeyboardInterrupt):
            print("")

    def handle_pack_menu(self) -> None:
        """Select default workflow pack."""
        print(f"\nCurrent Pack: {self.active_pack}")
        print("Available Packs:")
        print("  [1] developer     - Architecture, code patch implementation, test suite, review")
        print("  [2] business_idea - Startup checklist, critic gap analysis, strategy blueprint")
        print("  [3] qa            - Test matrix, edge cases, automation scripts, sign-off")
        try:
            choice = input("\nSelect pack (1-3): ").strip()
            mapping = {"1": "developer", "2": "business_idea", "3": "qa"}
            if choice in mapping:
                self.active_pack = mapping[choice]
                print(f"[OK] Active workflow pack set to: {self.active_pack}")
            else:
                print("Invalid choice.")
        except (EOFError, KeyboardInterrupt):
            print("")

    def execute_goal(self, goal: str) -> None:
        """Execute user goal through OrchestratorEngine with live feedback."""
        # Auto-detect intent if user specifies keywords
        goal_lower = goal.lower()
        selected_pack = self.active_pack
        if "business" in goal_lower or "market" in goal_lower or "monetiz" in goal_lower:
            selected_pack = "business_idea"
        elif "test" in goal_lower or "qa" in goal_lower or "coverage" in goal_lower:
            selected_pack = "qa"

        provider_name = self._get_active_provider_name()
        print(f"\n🚀 Dispatching Orchestrator on: '{goal}'")
        print(f"   Provider : {provider_name}")
        print(f"   Workflow : {selected_pack}")
        print("------------------------------------------------------------------------")

        def print_progress(text: str):
            print("\n" + text)

        engine = OrchestratorEngine(
            project_dir=self.project_dir,
            config=self.config,
            status_callback=print_progress
        )

        result = engine.run_pack(selected_pack, goal)

        print("\n" + "=" * 60)
        print("                ORCHESTRATOR RUN COMPLETE")
        print("=" * 60)
        print(result.summary_report)
        if result.blockers:
            print("\nBlockers / Warnings:")
            for b in result.blockers:
                print(f"  - {b}")
        print("=" * 60)

    def run(self) -> None:
        """Main REPL loop."""
        self.render_banner()

        while True:
            try:
                line = input("\norchestrator> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nExiting orchestrator. Goodbye!")
                break

            if not line:
                continue

            # If user just typed '/'
            if line == "/":
                self.print_slash_menu()
                continue

            # Slash commands
            if line.startswith("/"):
                cmd_parts = line.split()
                cmd = cmd_parts[0].lower()

                if cmd in ("/exit", "/quit"):
                    print("Exiting orchestrator. Goodbye!")
                    break
                elif cmd == "/clear":
                    os.system("clear" if os.name != "nt" else "cls")
                    self.render_banner()
                elif cmd == "/help":
                    self.print_slash_menu()
                elif cmd == "/status":
                    state = self.hub.read_state()
                    print("\n" + (state or "No active state found."))
                elif cmd == "/tasks":
                    self.handle_tasks_menu()
                elif cmd == "/provider":
                    self.handle_provider_menu()
                elif cmd == "/link":
                    self.handle_link_menu()
                elif cmd == "/pack":
                    self.handle_pack_menu()
                elif cmd == "/doctor":
                    report = SetupDoctor.render_report(self.config)
                    print("\n" + report)
                else:
                    print(f"Unknown command '{cmd}'. Available commands:")
                    self.print_slash_menu(cmd)

            else:
                # Natural language goal or request
                self.execute_goal(line)
