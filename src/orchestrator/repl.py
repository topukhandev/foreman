"""Interactive REPL shell with live slash suggestions and conversational intent handling."""

import os
import sys
import time
from typing import Dict, List, Optional
import uuid
import yaml
from orchestrator.config import load_config, OrchestratorConfig
from orchestrator.hub import Hub
from orchestrator.doctor import SetupDoctor
from orchestrator.links import WorkspaceLinks
from orchestrator.engine import OrchestratorEngine
from orchestrator.packs import PACKS
from orchestrator.adapters import create_adapter
from orchestrator.markdown import render_markdown
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
        self.config_mtime = os.path.getmtime(self.config_path) if os.path.exists(self.config_path) else 0.0
        self.visual_mode = getattr(self.config, "visual_terminal", True)
        self.active_pack = "developer"
        self.session_id = str(uuid.uuid4())
        self.session_turns = 0

        # Auto-clean any stale orphaned tasks left behind in state.md from earlier interrupted runs
        active_p = self._get_active_provider_name()
        self.hub.cleanup_stale_active_tasks(default_provider=active_p)

    def _reload_config_if_needed(self) -> None:
        """Reload config from disk if providers.yaml was modified externally."""
        if os.path.exists(self.config_path):
            try:
                curr_mtime = os.path.getmtime(self.config_path)
                if curr_mtime > self.config_mtime:
                    self.config = load_config(self.config_path)
                    self.config_mtime = curr_mtime
                    self.visual_mode = getattr(self.config, "visual_terminal", True)
            except Exception:
                pass

    def _get_active_provider_name(self) -> str:
        """Find currently active primary provider."""
        self._reload_config_if_needed()

        # 1. If explicit default_provider is configured and valid
        if getattr(self.config, "default_provider", ""):
            dp = self.config.default_provider
            if dp in self.config.providers:
                p = self.config.providers[dp]
                if p.enabled and p.verified:
                    return dp

        # 2. First enabled and verified provider
        for name, p in self.config.providers.items():
            if p.enabled and p.verified:
                return name

        # 3. First enabled provider (e.g. mock or unverified)
        for name, p in self.config.providers.items():
            if p.enabled:
                return name

        return "mock"

    def render_banner(self) -> None:
        """Render welcoming interactive session banner."""
        from orchestrator.terminal import detect_desktop_terminal
        active_p = self._get_active_provider_name()
        diag = SetupDoctor.check_system(self.config)
        available_tools = [
            t_name for t_name, t_info in diag["tools"].items() if t_info["installed"]
        ]
        services = self.links.list_links()
        services_str = f"{len(services)} linked" if services else "None linked"

        emulator = detect_desktop_terminal()
        if self.visual_mode and emulator:
            vis_str = f"Enabled ({emulator.name})"
        elif self.visual_mode:
            vis_str = "Enabled (No GUI display found)"
        else:
            vis_str = "Disabled (Headless background)"

        print("========================================================================")
        print(" 🎛️  FOREMAN ORCHESTRATOR — Front-Desk Session")
        print(f" Directory  : {self.project_dir}")
        print(f" Primary AI : {active_p}")
        print(f" Visual Term: {vis_str}")
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
            new_cfg["visual_terminal"] = self.visual_mode
            with open(self.config_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(new_cfg, f, sort_keys=False)
            self.config = load_config(self.config_path)
            self.config_mtime = os.path.getmtime(self.config_path)
            new_active = self._get_active_provider_name()

            # Immediately update hub/state.md so it never shows stale provider or running ghost tasks!
            self.hub.write_state(
                goal=self.hub.get_current_goal() or "Idle / Front-Desk Chat",
                phase="idle",
                active_tasks=[],
                blockers=[],
                default_provider=new_active
            )
            self.session_id = str(uuid.uuid4())
            self.session_turns = 0
            print(f"\n\033[1;32m[OK]\033[0m Switched default AI provider to: \033[1;36m{new_active}\033[0m\n")
        elif choice == "c":
            print("Cancelled.")
        else:
            print("Invalid selection.")

    def handle_visual_toggle(self, arg: str = "") -> None:
        """Toggle or set visual desktop terminal windows for AI workers."""
        from orchestrator.terminal import detect_desktop_terminal
        arg = arg.strip().lower()
        if arg in ("on", "1", "true", "enable", "show"):
            self.visual_mode = True
        elif arg in ("off", "0", "false", "disable", "hide", "headless"):
            self.visual_mode = False
        elif not arg:
            options = [
                ("1", f"Enable Visual Terminals  (Visible desktop windows) {'[CURRENT]' if self.visual_mode else ''}".strip()),
                ("2", f"Disable Visual Terminals (Run under the hood / Headless) {'[CURRENT]' if not self.visual_mode else ''}".strip()),
            ]
            choice = select_from_menu(f"? Configure AI Worker Terminal Visibility (Currently: {'ON' if self.visual_mode else 'OFF'}):", options)
            if not choice:
                return
            self.visual_mode = (choice == "1")
        else:
            self.visual_mode = not self.visual_mode

        # Persist visual_terminal setting to providers.yaml
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                data["visual_terminal"] = self.visual_mode
                with open(self.config_path, "w", encoding="utf-8") as f:
                    yaml.safe_dump(data, f, sort_keys=False)
                self.config.visual_terminal = self.visual_mode
                self.config_mtime = os.path.getmtime(self.config_path)
            except Exception:
                pass

        # Update hub/state.md with worker visibility
        self.hub.write_state(
            goal=self.hub.get_current_goal() or "Idle / Front-Desk Chat",
            phase="idle",
            active_tasks=[],
            blockers=[],
            default_provider=self._get_active_provider_name(),
            visual_mode=self.visual_mode
        )

        emulator = detect_desktop_terminal()
        status_str = "\033[1;32mENABLED (Visible Desktop Windows)\033[0m" if self.visual_mode else "\033[1;33mDISABLED (Headless Under The Hood)\033[0m"
        print(f"\n🖥️  Visual Worker Terminals: {status_str}")
        if self.visual_mode:
            if emulator:
                print(f"   Desktop Terminal : \033[1;36m{emulator.name}\033[0m ({emulator.executable})")
                print("   AI workers (OpenCode, Antigravity, Claude) will pop up in visible desktop windows.")
            else:
                print("   ⚠️  Warning: No desktop display or terminal emulator detected (will fall back to headless).")
        else:
            print("   AI workers will execute completely under the hood (headless in background).")

    def _detect_visual_intent(self, text: str) -> Optional[str]:
        """Detect natural language intent to configure visual worker terminal windows."""
        t = text.strip().lower()
        keywords = [
            "visual terminal", "terminal visibility", "desktop terminal",
            "ভিজ্যুয়াল টার্মিনাল", "ভিজ্যুয়াল টার্মিনাল", "টার্মিনাল দেখা",
            "আন্ডার দ্য হুড", "under the hood", "headless mode", "headless"
        ]
        if not any(k in t for k in keywords):
            return None

        words = set(t.split())
        on_triggers = ["on", "enable", "show", "open", "দেখতে", "দেখা যাবে", "অন"]
        off_triggers = ["off", "disable", "hide", "close", "বন্ধ", "অফ", "hood", "হুড", "headless", "দেখা যাবে না", "না যেন"]

        if any(tr in t for tr in off_triggers) or "off" in words or "hide" in words:
            return "off"
        elif any(tr in t for tr in on_triggers) or "on" in words or "show" in words:
            return "on"
        return ""  # Trigger menu

    def _detect_provider_intent(self, text: str) -> Optional[str]:
        """Detect if user is expressing intent to switch or select AI provider in natural language."""
        t = text.strip().lower()
        triggers = [
            "change provider", "switch provider", "use provider", "select provider",
            "set provider", "choose provider", "change ai", "switch ai", "use ai",
            "switch to opencode", "change to opencode", "use opencode",
            "switch to claude", "change to claude", "use claude",
            "switch to antigravity", "change to antigravity", "use antigravity",
            "switch to agy", "change to agy", "use agy",
            "switch to mock", "use mock",
            "প্রোভাইডার চেঞ্জ", "প্রোভাইডার পরিবর্তন", "প্রোভাইডার বদল"
        ]
        if any(tr in t for tr in triggers):
            if "opencode" in t:
                return "opencode"
            elif "claude" in t:
                return "claude"
            elif "agy" in t or "antigravity" in t:
                return "antigravity"
            elif "mock" in t or "simul" in t:
                return "mock"
            elif "dual" in t:
                return "dual"
            return ""  # Trigger interactive selector menu
        return None

    def handle_new_session(self) -> None:
        """Start a fresh conversation thread with active AI worker."""
        self.session_id = str(uuid.uuid4())
        self.session_turns = 0
        print("\n\033[1;32m[OK]\033[0m Started a fresh conversation thread with active AI worker.\n")

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
                print("\n" + render_markdown(spec))
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
            services = self.links.list_links()
            lines = [f"📍 **Working Directory**: `{self.project_dir}`"]
            if services:
                lines.append("\n### Connected Workspaces")
                for s in services:
                    lines.append(f"* **{s.name}**: `{s.path}` ({s.description or 'No description'})")
            else:
                lines.append("*(No external services linked yet. Use `/link` to connect one.)*")
            print("\n" + render_markdown("\n".join(lines)))
            return True

        # Who are you / help
        if q in ("who are you", "who r u", "what is orchestrator", "what are you", "hello", "hi", "hey"):
            active_p = self._get_active_provider_name()
            content = (
                f"### 👋 Foreman Orchestrator — Front-Desk Bridge & Conductor\n"
                f"* **Active AI Worker**: `{active_p}`\n"
                f"* **Direct AI Chat**: Ask questions, brainstorm, or explore codebase naturally.\n"
                f"* **Autonomous Sprint**: Run `/run <goal>` to dispatch a complete 4-agent sprint.\n"
                f"* **Menu & Tools**: Type `/` to open live dropdown (`/provider`, `/status`, `/link`, `/doctor`)."
            )
            print("\n" + render_markdown(content))
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

        if self.session_turns > 0:
            prompt = (
                f"[Workspace Update: {self.project_dir} | State: {state_content[:600]}]\n"
                f"User Request: {query}"
            )
            session_arg = self.session_id if p_name == "claude_code" else "continue"
        else:
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
            session_arg = self.session_id if p_name == "claude_code" else None

        adapter = create_adapter(p_name, p_cfg, timeout_sec=self.config.budget.max_time_per_task_sec)
        adapter.visual_mode = self.visual_mode
        result = adapter.run(
            prompt=prompt,
            cwd=self.project_dir,
            task_id="front_desk",
            session_id=session_arg,
            show_progress=True,
            progress_label=p_name
        )

        if result.success and result.output:
            self.session_turns += 1
            formatted = render_markdown(result.output.strip())
            print(formatted)
        elif result.error:
            print(f"\n\033[1;31m[Worker Error]\033[0m {result.error}\n")
        else:
            print("\n(No response received from AI worker.)\n")

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

        confirm = read_input_with_esc("\nProceed with multi-agent sprint? [Y/n]: ", default="y")
        if not confirm or confirm.lower() not in ("y", "yes"):
            print("Execution cancelled.")
            return

        print("\n🚀 Starting multi-agent sprint...")
        print("------------------------------------------------------------------------")

        def print_progress(dashboard: str, event: Optional[str] = None):
            if event:
                print(f"  {event}")

        engine = OrchestratorEngine(
            project_dir=self.project_dir,
            config=self.config,
            status_callback=print_progress
        )
        for adapter in engine.adapters.values():
            adapter.visual_mode = self.visual_mode

        result = engine.run_pack(selected_pack, goal)

        print("\n" + "=" * 60)
        print("                ORCHESTRATOR RUN COMPLETE")
        print("=" * 60)
        print("\n" + render_markdown(result.summary_report))
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
                active_p = self._get_active_provider_name()
                prompt = f"\033[1;36m❯\033[0m \033[1morchestrator\033[0m \033[1;33m[{active_p}]\033[0m › "
                line = read_interactive_line(prompt=prompt)
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

                # Auto-expand unambiguous slash command prefix (e.g. /pro -> /provider, /st -> /status)
                all_cmds = ["/run", "/new", "/provider", "/visual", "/status", "/tasks", "/link", "/doctor", "/clear", "/help", "/exit", "/quit"]
                matches = [c for c in all_cmds if c.startswith(cmd)]
                if len(matches) == 1:
                    cmd = matches[0]

                if cmd in ("/exit", "/quit"):
                    print("Exiting orchestrator. Goodbye!")
                    break
                elif cmd == "/run":
                    self.handle_run_command(arg)
                elif cmd == "/new":
                    self.handle_new_session()
                elif cmd == "/clear":
                    os.system("clear" if os.name != "nt" else "cls")
                    self.render_banner()
                elif cmd == "/help":
                    self.print_slash_menu()
                elif cmd == "/status":
                    state = self.hub.read_state()
                    print("\n" + render_markdown(state or "No active state found."))
                elif cmd == "/tasks":
                    self.handle_tasks_menu()
                elif cmd == "/provider":
                    self.handle_provider_menu(cmd_parts[1] if len(cmd_parts) > 1 else "")
                elif cmd == "/visual":
                    self.handle_visual_toggle(cmd_parts[1] if len(cmd_parts) > 1 else "")
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
                # 1. Check for natural language provider switch/selection intent
                provider_intent = self._detect_provider_intent(line)
                if provider_intent is not None:
                    self.handle_provider_menu(provider_intent)
                    continue

                # 2. Check for natural language visual terminal configuration intent
                visual_intent = self._detect_visual_intent(line)
                if visual_intent is not None:
                    self.handle_visual_toggle(visual_intent)
                    continue

                # 3. Fast local operational query (who are you, where am i)
                if self.handle_lightweight_query(line):
                    continue

                # 3. If user specifically asks to run/build/implement a full project feature:
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

                # 4. Natural Language query -> Bridge directly to active AI Provider!
                self.bridge_to_ai_provider(line)
