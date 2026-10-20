"""Interactive REPL shell with live slash suggestions and conversational intent handling."""

import os
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
from orchestrator.adapters import create_adapter
from orchestrator.input_reader import (
    read_interactive_line,
    COMMANDS,
    render_dropdown_box,
    select_from_menu,
    read_input_with_esc
)


class InteractiveShell:
    """Claude Code / Antigravity style interactive terminal console."""

    def __init__(self, project_dir: str = "."):
        self.project_dir = os.path.abspath(project_dir)
        self.hub = Hub(self.project_dir)
        self.links = WorkspaceLinks(self.project_dir)
        self.config_path = os.path.join(self.project_dir, "providers.yaml")
        self.config = load_config(self.config_path)
        self.active_pack = "developer"

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
        print(" Type '/' to see live commands, or ask any question/goal below.")
        print(" Type /help for usage guide, /exit to quit.")
        print("========================================================================")

    def print_slash_menu(self, filter_prefix: str = "") -> None:
        """Render dropdown menu of available slash commands."""
        box_str, _, _ = render_dropdown_box(filter_prefix or "/")
        if box_str:
            print(box_str)

    def handle_provider_menu(self, choice_arg: str = "") -> None:
        """Interactive provider selector menu or direct switch."""
        diag = SetupDoctor.check_system(self.config)
        tools = diag["tools"]

        has_agy = tools.get("antigravity_cli", {}).get("installed", False)
        has_claude = tools.get("claude_code", {}).get("installed", False)
        has_opencode = tools.get("opencode", {}).get("installed", False)

        curr = self._get_active_provider_name()

        preset_map = {"1": "2", "2": "3", "3": "5", "4": "4", "5": "6"}
        name_alias = {
            "agy": "1", "antigravity": "1", "antigravity_cli": "1",
            "claude": "2", "claude_code": "2",
            "opencode": "3",
            "dual": "4",
            "mock": "5", "sim": "5"
        }

        choice = choice_arg.lower().strip()
        if choice in name_alias:
            choice = name_alias[choice]

        if not choice:
            options = [
                ("1", f"Antigravity CLI (agy)       {'[INSTALLED]' if has_agy else '[NOT FOUND]'} {'[ACTIVE]' if curr == 'antigravity_cli' else ''}".strip()),
                ("2", f"Claude Code (claude)        {'[INSTALLED]' if has_claude else '[NOT FOUND]'} {'[ACTIVE]' if curr == 'claude_code' else ''}".strip()),
                ("3", f"OpenCode (opencode)         {'[INSTALLED]' if has_opencode else '[NOT FOUND]'} {'[ACTIVE]' if curr == 'opencode' else ''}".strip()),
                ("4", f"Dual-Model (Antigravity builds, Claude reviews) {'[READY]' if (has_agy and has_claude) else '[NEED BOTH]'}"),
                ("5", "Simulation / Mock Mode (Zero tokens)")
            ]
            choice = select_from_menu("? Select Default AI Provider for this project:", options)
            if not choice:
                return

        from orchestrator.wizard import build_config_for_preset
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

    def handle_link_menu(self, args: Optional[List[str]] = None) -> None:
        """Interactive link menu to connect another directory/service, or CLI command."""
        services = self.links.list_links()

        # If direct arguments given: /link add <path> [name] [desc] or /link remove <name>
        if args and len(args) > 0:
            sub = args[0].lower()
            if sub == "list":
                if services:
                    print("\nCurrently Linked Services:")
                    for s in services:
                        print(f"  - {s.name}: {s.path} ({s.description or 'No desc'})")
                else:
                    print("\nNo services currently linked.")
                return
            elif sub == "add" and len(args) > 1:
                target_path = args[1]
                name = args[2] if len(args) > 2 else ""
                desc = " ".join(args[3:]) if len(args) > 3 else ""
                ok = self.links.add_link(name, target_path, desc)
                if ok:
                    print(f"[OK] Successfully linked directory at: {os.path.abspath(os.path.expanduser(target_path))}")
                else:
                    print(f"[!] Error: Directory not found: {target_path}")
                return
            elif sub in ("remove", "rm") and len(args) > 1:
                del_name = args[1]
                if self.links.remove_link(del_name):
                    print(f"[OK] Removed link: {del_name}")
                else:
                    print(f"[!] Link not found: {del_name}")
                return
            elif len(args) == 1 and os.path.exists(os.path.expanduser(args[0])):
                # Quick syntax: /link /path/to/dir
                target_path = args[0]
                ok = self.links.add_link("", target_path, "")
                if ok:
                    print(f"[OK] Successfully linked directory at: {os.path.abspath(os.path.expanduser(target_path))}")
                return

        if services:
            print("\nCurrently Linked Services:")
            for s in services:
                print(f"  - {s.name}: {s.path} ({s.description or 'No desc'})")

        options = [
            ("1", "Add new linked folder"),
            ("2", "Remove existing link")
        ]
        action = select_from_menu("? Connect an external folder/service:", options)
        if not action:
            return

        if action == "1":
            target_path = read_input_with_esc("Enter path to directory (e.g. ~/Desktop/api): ")
            if not target_path:
                return
            name = read_input_with_esc("Enter a service name (e.g. backend, ui, docs) [default: auto]: ")
            if name is None:
                return
            desc = read_input_with_esc("Brief description of this service: ")
            if desc is None:
                return
            ok = self.links.add_link(name, target_path, desc)
            if ok:
                print(f"[OK] Successfully linked service '{name}' at: {os.path.abspath(os.path.expanduser(target_path))}")
            else:
                print(f"[!] Error: Directory not found: {target_path}")
        elif action == "2":
            if not services:
                print("No services currently linked.")
                return
            del_name = read_input_with_esc("Enter service name to remove: ")
            if not del_name:
                return
            if self.links.remove_link(del_name):
                print(f"[OK] Removed link: {del_name}")
            else:
                print(f"[!] Link not found: {del_name}")

    def handle_tasks_menu(self) -> None:
        """Inspect task status and contents interactively."""
        statuses = self.hub.get_all_task_statuses()
        if not statuses:
            print("\n[!] No tasks found in this project workspace yet.")
            return

        print(f"\nTask History ({len(statuses)} tasks):")
        for s in statuses:
            print(f"  Task #{s.task_id}: {s.title} [{s.role}] -> {s.state.upper()} ({s.one_line_summary})")

        pick = read_input_with_esc("\nEnter Task ID to view full specification & results (or Esc to back): ")
        if pick:
            spec = self.hub.read_task_spec(pick)
            if spec:
                print("\n" + "=" * 60)
                print(spec)
                print("=" * 60)
            else:
                print(f"Task #{pick} not found.")

    def handle_pack_menu(self, choice_arg: str = "") -> None:
        """Select default workflow pack interactively or via argument."""
        mapping = {
            "1": "developer", "dev": "developer", "developer": "developer",
            "2": "business_idea", "business": "business_idea", "biz": "business_idea", "idea": "business_idea",
            "3": "qa", "test": "qa"
        }

        choice = choice_arg.lower().strip()
        if choice in mapping:
            self.active_pack = mapping[choice]
            print(f"[OK] Active workflow pack set to: {self.active_pack}")
            return

        options = [
            ("1", "developer     - Architecture, code patch implementation, test suite, review"),
            ("2", "business_idea - Startup checklist, critic gap analysis, strategy blueprint"),
            ("3", "qa            - Test matrix, edge cases, automation scripts, sign-off")
        ]
        choice = select_from_menu(f"? Select Workflow Pack (Current: {self.active_pack}):", options)
        if choice and choice in mapping:
            self.active_pack = mapping[choice]
            print(f"[OK] Active workflow pack set to: {self.active_pack}")

    def check_or_prompt_ai_provider(self) -> bool:
        """
        Verify that a real AI CLI worker (antigravity_cli, claude_code, opencode) is connected.
        If not, suggest and prompt the user to select one before executing natural language.
        """
        active_p = self._get_active_provider_name()
        if active_p != "mock":
            return True

        diag = SetupDoctor.check_system(self.config)
        tools = diag["tools"]
        installed_any = any(t["installed"] for t in tools.values())

        print("\n\033[1;33m⚠️  No Active AI Provider Selected!\033[0m")
        print("Foreman Orchestrator is a Front-Desk Bridge & Conductor — it has no built-in LLM.")
        print("To converse, inspect workspace state, and write code, an AI worker CLI must be connected.")

        if not installed_any:
            print("\n[!] No local AI CLI tools were detected on your system.")
            print("    Please install Antigravity CLI (agy) or Claude Code (claude).")
            print("    Run /doctor for setup diagnostics and instructions.")
            return False

        print("\nPlease choose an AI Worker to activate for this project:")
        self.handle_provider_menu()
        return self._get_active_provider_name() != "mock"

    def handle_lightweight_query(self, query: str) -> bool:
        """
        Fast local utility responses for zero-token operational questions.
        Returns True if handled locally, False if it requires AI worker processing.
        """
        q = query.strip().lower()

        # Location / Folder
        if q in ("where am i", "pwd", "which folder is this", "what directory is this", "which folder", "what folder"):
            print(f"\n📍 Working Directory: {self.project_dir}")
            services = self.links.list_links()
            if services:
                print("   Connected Workspaces:")
                for s in services:
                    print(f"   - {s.name}: {s.path} ({s.description or 'No desc'})")
            else:
                print("   (No external services linked yet. Use /link to connect one.)")
            return True

        # Who are you / help
        if q in ("who are you", "who r u", "what is orchestrator", "what are you", "hello", "hi", "hey"):
            active_p = self._get_active_provider_name()
            print(f"\n👋 Foreman Orchestrator — Front-Desk Bridge & Multi-Agent Conductor")
            print(f"   - Active AI Worker: {active_p} (Pack: {self.active_pack})")
            print("   - Type your question or goal naturally — your active AI worker will respond directly.")
            print("   - Type /run <goal> to execute a complete multi-agent engineering sprint.")
            print("   - Type '/' to open the live command dropdown (/provider, /status, /link, /doctor).")
            return True

        return False

    def bridge_to_ai_provider(self, query: str) -> None:
        """
        Send user query/instruction directly to active AI provider (agy, claude, opencode)
        with workspace context and live elapsed timer.
        """
        if not self.check_or_prompt_ai_provider():
            return

        p_name = self._get_active_provider_name()
        p_cfg = self.config.providers.get(p_name)
        if not p_cfg:
            print(f"[!] Configuration for '{p_name}' not found.")
            return

        # Prepare workspace context for the AI
        state_content = self.hub.read_state() or "Project state is clean / idle."
        services = self.links.list_links()
        links_summary = ", ".join([f"{s.name} at {s.path}" for s in services]) if services else "None"

        prompt = (
            f"You are the Front-Desk AI Worker for Foreman Orchestrator.\n"
            f"Project Directory: {self.project_dir}\n"
            f"Connected Workspaces: {links_summary}\n"
            f"Current Project State:\n{state_content[:2000]}\n\n"
            f"User Inquiry / Request: {query}\n\n"
            f"Instructions:\n"
            f"1. Answer the user directly, clearly, and concisely based on the project context above.\n"
            f"2. If the user asks about project state or progress, summarize accurately from the state above.\n"
            f"3. If the user is proposing a software feature or engineering goal, provide a crisp answer or outline, "
            f"and inform them they can run '/run <goal>' to trigger a complete 4-agent sprint."
        )

        adapter = create_adapter(p_name, p_cfg, timeout_sec=self.config.budget.max_time_per_task_sec)
        result = adapter.run(
            prompt=prompt,
            cwd=self.project_dir,
            task_id="front_desk",
            show_progress=True,
            progress_label=p_name
        )

        if result.success and result.output:
            print(result.output.strip())
        elif result.error:
            print(f"\n\033[1;31m[Worker Error]\033[0m {result.error}")
        else:
            print("\n(No response received from AI worker.)")

    def execute_goal(self, goal: str) -> None:
        """Execute full multi-agent workflow sprint on user goal with confirmation."""
        goal_lower = goal.lower()
        selected_pack = self.active_pack

        if any(w in goal_lower for w in ["business", "market", "monetiz", "pricing", "competitor", "idea"]):
            selected_pack = "business_idea"
        elif any(w in goal_lower for w in ["test", "qa", "coverage", "benchmark", "stress"]):
            selected_pack = "qa"

        provider_name = self._get_active_provider_name()

        # Ask for confirmation before launching an engineering sprint!
        print(f"\n📋 Proposed Multi-Agent Sprint: '{goal}'")
        print(f"   Target Dir : {self.project_dir}")
        print(f"   AI Worker  : {provider_name}")
        print(f"   Workflow   : {selected_pack}")

        confirm = read_input_with_esc("\nProceed with multi-agent sprint? [Y/n]: ", default="y")
        if not confirm or confirm.lower() not in ("y", "yes"):
            print("Execution cancelled.")
            return

        print("\n🚀 Starting multi-agent sprint...")
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

    def handle_run_command(self, goal: str = "") -> None:
        """Launch an engineering sprint via /run <goal>."""
        if not goal:
            goal = read_input_with_esc("\nEnter goal for multi-agent sprint: ")
        if not goal:
            print("No goal specified. Cancelled.")
            return

        if not self.check_or_prompt_ai_provider():
            return

        self.execute_goal(goal)

    def run(self) -> None:
        """Main interactive REPL loop."""
        self.render_banner()

        while True:
            try:
                print()
                line = read_interactive_line()
            except (EOFError, KeyboardInterrupt):
                print("\nExiting orchestrator. Goodbye!")
                break

            if not line:
                continue

            # Slash commands
            if line.startswith("/"):
                cmd_parts = line.split()
                cmd = cmd_parts[0].lower()
                arg = " ".join(cmd_parts[1:]) if len(cmd_parts) > 1 else ""

                if cmd in ("/exit", "/quit"):
                    print("Exiting orchestrator. Goodbye!")
                    break
                elif cmd == "/run":
                    self.handle_run_command(arg)
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
                    self.handle_provider_menu(cmd_parts[1] if len(cmd_parts) > 1 else "")
                elif cmd == "/link":
                    self.handle_link_menu(cmd_parts[1:])
                elif cmd == "/pack":
                    self.handle_pack_menu(cmd_parts[1] if len(cmd_parts) > 1 else "")
                elif cmd == "/doctor":
                    report = SetupDoctor.render_report(self.config)
                    print("\n" + report)
                else:
                    print(f"Unknown command '{cmd}'. Available commands:")
                    self.print_slash_menu(cmd)

            else:
                # 1. Fast local operational query (who are you, where am i)
                if self.handle_lightweight_query(line):
                    continue

                # 2. If user specifically asks to run/build/implement a full project feature:
                lower = line.strip().lower()
                if (lower.startswith("run ") or lower.startswith("build ") or 
                    lower.startswith("implement ") or lower.startswith("create a ")):
                    options = [
                        ("1", "Run full 4-agent sprint (Planner -> Critic -> Builder -> Reviewer)"),
                        ("2", "Ask AI worker directly (Chat / Consult)")
                    ]
                    choice = select_from_menu(f"? Detected action request: '{line}'", options, default_index=1)
                    if not choice:
                        continue
                    if choice == "1":
                        self.execute_goal(line)
                        continue

                # 3. Natural Language query -> Bridge directly to active AI Provider!
                self.bridge_to_ai_provider(line)
