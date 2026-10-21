"""Interactive REPL shell with live slash suggestions and conversational intent handling."""

import os
import re
import shlex
import shutil
import signal
import sys
import textwrap
import time
from typing import Any, Dict, List, Optional, Tuple
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


def parse_spawn_args(raw_str: str, default_provider: str = "antigravity_cli", default_dir: str = ".") -> Optional[Dict[str, Any]]:
    """
    Parse a single spawn specification string into {"provider": str, "count": int, "dir": str}.
    Handles inputs like:
      - "opencode /home/user/Desktop/project"
      - "agy /home/user/projects/ai\\ projects"
      - "claude ~/Desktop/image"
      - "2 opencode"
      - "3"
      - "/home/user/projects"
    """
    raw_str = raw_str.strip()
    if not raw_str:
        return None

    try:
        parts = shlex.split(raw_str)
    except Exception:
        parts = raw_str.split()

    if not parts:
        return None

    tool_aliases = {
        "opencode": "opencode", "opencode_ai": "opencode", "ওপেনকোড": "opencode", "ওপেন কোড": "opencode",
        "agy": "antigravity_cli", "antigravity": "antigravity_cli", "antigravity_cli": "antigravity_cli",
        "এজিআই": "antigravity_cli", "অ্যান্টিগ্রাভিটি": "antigravity_cli",
        "claude": "claude_code", "claude_code": "claude_code", "cluade": "claude_code",
        "ক্লড": "claude_code",
        "bash": "bash", "terminal": "bash", "shell": "bash", "sh": "bash"
    }

    count = 1
    tool = default_provider
    dir_path = default_dir

    idx = 0
    # 1. Check if first token is a count
    if idx < len(parts) and parts[idx].isdigit():
        count = max(1, int(parts[idx]))
        idx += 1

    # 2. Check if current token is a tool
    if idx < len(parts) and parts[idx].lower().rstrip(":,") in tool_aliases:
        tool = tool_aliases[parts[idx].lower().rstrip(":,")]
        idx += 1
        # Check if count comes after tool: e.g. "opencode 2 /path"
        if idx < len(parts) and parts[idx].isdigit():
            count = max(1, int(parts[idx]))
            idx += 1

    # 3. Remaining tokens represent directory path
    if idx < len(parts):
        dir_candidate = " ".join(parts[idx:]).strip()
        dir_candidate = dir_candidate.strip("'\"").replace("\\ ", " ")
        if dir_candidate:
            if dir_candidate.startswith("home/"):
                dir_candidate = "/" + dir_candidate
            resolved_dir = os.path.abspath(os.path.expanduser(dir_candidate))
            if not os.path.isdir(resolved_dir):
                parent = os.path.dirname(resolved_dir)
                target_base = os.path.basename(resolved_dir).lower().replace(" ", "").replace("-", "").replace("_", "")
                if os.path.isdir(parent):
                    for entry in os.listdir(parent):
                        cand_clean = entry.lower().replace(" ", "").replace("-", "").replace("_", "")
                        if cand_clean == target_base and os.path.isdir(os.path.join(parent, entry)):
                            resolved_dir = os.path.join(parent, entry)
                            break
            dir_path = resolved_dir

    return {
        "provider": tool,
        "count": count,
        "dir": os.path.abspath(os.path.expanduser(dir_path))
    }


def extract_spawn_directives(text: str, default_provider: str = "antigravity_cli", default_dir: str = ".") -> List[Dict[str, Any]]:
    """
    Extract all /spawn or ACTION: SPAWN commands from text output.
    Supports markdown blocks, bulleted lists, and inline directives.
    """
    pattern = r"(?:/spawn|ACTION:\s*SPAWN)\s+([^\n\r`\"]+)"
    specs: List[Dict[str, Any]] = []

    for match in re.finditer(pattern, text, re.IGNORECASE):
        raw = match.group(1).strip()
        # Ignore template placeholders like [tool] [dir] or [count]
        if "[" in raw or "]" in raw or "<" in raw:
            continue
        spec = parse_spawn_args(raw, default_provider=default_provider, default_dir=default_dir)
        if spec:
            specs.append(spec)

    return specs


def clean_user_facing_output(text: str) -> str:
    """
    Filter out internal /spawn directives and code blocks that contain only spawn directives,
    so the user gets a concise, high-signal explanation without internal CLI plumbing commands.
    """
    lines = text.splitlines()
    filtered = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Skip isolated ```bash /spawn ... ``` blocks
        if line.strip().startswith("```"):
            j = i + 1
            block_lines = []
            while j < len(lines) and not lines[j].strip().startswith("```"):
                block_lines.append(lines[j])
                j += 1
            if j < len(lines):
                all_spawn = all(
                    re.search(r"^\s*(?:/spawn|ACTION:\s*SPAWN)\b", bl, re.IGNORECASE) or not bl.strip()
                    for bl in block_lines
                ) and any(re.search(r"^\s*(?:/spawn|ACTION:\s*SPAWN)\b", bl, re.IGNORECASE) for bl in block_lines)
                if all_spawn:
                    i = j + 1
                    continue
        # Skip single spawn line
        if re.search(r"^\s*(?:/spawn|ACTION:\s*SPAWN)\b", line, re.IGNORECASE):
            i += 1
            continue
        filtered.append(line)
        i += 1
    return "\n".join(filtered).strip()


def print_claude_error(error_msg: str) -> None:
    """Render a clean, low-verbosity Claude Code style error container."""
    lines = error_msg.strip().splitlines()
    cols = shutil.get_terminal_size(fallback=(80, 24)).columns
    box_w = max(40, min(cols - 4, 76))
    inner_w = box_w - 4
    top = f"\033[90m╭─\033[1;31m Error \033[0;90m{'─' * max(0, box_w - 11)}╮\033[0m"
    bot = f"\033[90m╰{'─' * (box_w - 2)}╯\033[0m"
    print(f"\n{top}")
    for l in lines[:10]:
        for wrapped in textwrap.wrap(l, width=inner_w) or [""]:
            pad = " " * max(0, inner_w - len(wrapped))
            print(f"\033[90m│\033[0m  \033[1;31m{wrapped}\033[0m{pad}\033[90m│\033[0m")
    if len(lines) > 10:
        print(f"\033[90m│\033[0m  \033[2;90m... ({len(lines) - 10} more lines truncated)\033[0m{' ' * max(0, inner_w - 32)}\033[90m│\033[0m")
    print(f"{bot}\n")


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
        self.effort = getattr(self.config, "effort", "medium")
        self.last_duration_sec: Optional[float] = None
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
                    self.effort = getattr(self.config, "effort", "medium")
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
        """Render clean, modern Claude Code style session banner."""
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
            vis_str = f"\033[1;32mON\033[0m \033[90m({emulator.name})\033[0m"
        elif self.visual_mode:
            vis_str = "\033[1;33mON (No GUI display)\033[0m"
        else:
            vis_str = "\033[90mOFF (Headless)\033[0m"

        cols = shutil.get_terminal_size(fallback=(80, 24)).columns
        box_w = max(44, min(cols - 4, 72))
        inner_w = box_w - 4

        home = os.path.expanduser("~")
        disp_dir = ("~" + self.project_dir[len(home):]) if self.project_dir.startswith(home) else self.project_dir
        if len(disp_dir) > inner_w - 12:
            disp_dir = "..." + disp_dir[-(inner_w - 15):]
        tools_str = ", ".join(available_tools) if available_tools else "none"

        top = f"\033[90m╭{'─' * (box_w - 2)}╮\033[0m"
        bot = f"\033[90m╰{'─' * (box_w - 2)}╯\033[0m"

        def _row(text: str) -> str:
            clean_len = len(re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text))
            pad = " " * max(0, inner_w - clean_len)
            return f"\033[90m│\033[0m  {text}{pad}\033[90m│\033[0m"

        banner_lines = [
            top,
            _row("\033[1;37mForeman Orchestrator\033[0m"),
            _row("\033[90mAutonomous multi-provider AI coordinator & front desk\033[0m"),
            _row(""),
            _row(f"\033[36mcwd\033[0m     : \033[1;33m{disp_dir}\033[0m"),
            _row(f"\033[36mworker\033[0m  : \033[1;36m{active_p}\033[0m \033[90m•\033[0m \033[36meffort\033[0m: \033[1;34m{self.effort.upper()}\033[0m \033[90m•\033[0m \033[36mvisual\033[0m: {vis_str}"),
            _row(f"\033[36mtools\033[0m   : \033[90m{tools_str}\033[0m"),
            _row(f"\033[36mlinks\033[0m   : \033[90m{services_str}\033[0m"),
            _row(""),
            _row("\033[90mType \033[1;37m/\033[0;90m for menu, \033[1;37m/run\033[0;90m for sprint, or ask any question below\033[0m"),
            bot
        ]
        print("\n" + "\n".join(banner_lines))

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
            from orchestrator.terminal import close_all_visual_sessions
            close_all_visual_sessions()
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

        if not self.visual_mode:
            from orchestrator.terminal import close_all_visual_sessions
            close_all_visual_sessions()

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

    def handle_effort_menu(self, choice_arg: str = "") -> None:
        """Interactive reasoning effort level selector (low, medium, high) or direct switch."""
        options_map = {
            "1": "low", "low": "low", "l": "low", "fast": "low",
            "2": "medium", "medium": "medium", "med": "medium", "m": "medium", "normal": "medium", "default": "medium",
            "3": "high", "high": "high", "h": "high", "deep": "high", "max": "high"
        }
        choice = choice_arg.lower().strip()
        if choice in options_map:
            new_effort = options_map[choice]
        else:
            options = [
                ("1", f"low    - Fast, lightweight reasoning & low token cost {'[ACTIVE]' if self.effort == 'low' else ''}".strip()),
                ("2", f"medium - Balanced reasoning & execution (Default)     {'[ACTIVE]' if self.effort == 'medium' else ''}".strip()),
                ("3", f"high   - Deep reasoning & exhaustive verification     {'[ACTIVE]' if self.effort == 'high' else ''}".strip())
            ]
            pick = select_from_menu(f"? Select AI Reasoning Effort Level (Current: {self.effort.upper()}):", options)
            if not pick:
                return
            new_effort = options_map.get(pick, "medium")

        self.effort = new_effort
        # Persist effort in providers.yaml
        try:
            cfg_dict = {}
            if os.path.exists(self.config_path):
                with open(self.config_path, "r", encoding="utf-8") as f:
                    cfg_dict = yaml.safe_load(f) or {}
            cfg_dict["effort"] = self.effort
            with open(self.config_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(cfg_dict, f, sort_keys=False)
            self.config = load_config(self.config_path)
            self.config_mtime = os.path.getmtime(self.config_path)
        except Exception:
            pass

        effort_desc = {
            "low": "Fast reasoning mode (Flash-Lite / Haiku / low token cost)",
            "medium": "Balanced reasoning mode (Flash / Sonnet / standard execution)",
            "high": "Deep thinking mode (Pro / Reasoning / exhaustive edge-case verification)"
        }
        print(f"\n\033[1;32m[OK]\033[0m Reasoning effort set to: \033[1;34m{self.effort.upper()}\033[0m — {effort_desc.get(self.effort, '')}\n")

    def handle_close_terminals(self) -> None:
        """Instantly close all active desktop terminal windows and persistent sessions."""
        start_t = time.time()
        from orchestrator.terminal import close_all_visual_sessions, close_all_spawned_terminals, list_active_terminals
        try:
            active_cnt = len(list_active_terminals())
        except Exception:
            active_cnt = 0
        close_all_visual_sessions()
        close_all_spawned_terminals()
        elapsed = time.time() - start_t
        self.last_duration_sec = elapsed
        print("\n\033[1;36m⏺ Action:\033[0m Close Terminals")
        print(f"  \033[90m↳ Closed  :\033[0m \033[1;37m{active_cnt} active terminal window(s)\033[0m")
        print(f"  \033[90m↳ Duration:\033[0m {elapsed:.2f}s")
        print("  \033[1;32m✓ Terminals closed successfully.\033[0m\n")

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
    def open_requested_terminals(self, specs: List[Dict[str, Any]]) -> None:
        """
        Intelligently open requested worker terminals without rigid menus, commands, or fake wrappers.
        Directly checks System Resource Guard and launches clean desktop terminals.
        """
        from orchestrator.terminal import spawn_terminals_batch, detect_desktop_terminal
        from orchestrator.system_guard import SystemGuard

        emulator = detect_desktop_terminal()
        if not emulator:
            print("\n\033[1;33m⚠️  No Desktop GUI Terminal Found\033[0m")
            print("Cannot launch visible desktop terminal windows in headless mode (no DISPLAY / WAYLAND).")
            return

        if not specs:
            return

        total_terminals = sum(s.get("count", 1) for s in specs)
        target_dir = specs[0].get("dir", self.project_dir)

        # Pre-flight hardware guard check to prevent machine freezing / crashing
        can_spawn, guard_msg = SystemGuard.can_spawn(total_terminals, target_dir=target_dir)
        if not can_spawn:
            print(f"\n{guard_msg}\n")
            return

        spawned, errors = spawn_terminals_batch(specs)

        if errors:
            for err in errors:
                print(f"\n{err}")

        if spawned:
            print("\n\033[1;36m⏺ Action:\033[0m Launch Worker Terminal(s)")
            home = os.path.expanduser("~")
            counts: Dict[str, int] = {}
            tool_dirs: Dict[str, str] = {}
            for t in spawned:
                counts[t.tool_name] = counts.get(t.tool_name, 0) + 1
                tool_dirs[t.tool_name] = t.cwd
                cwd_disp = ("~" + t.cwd[len(home):]) if t.cwd.startswith(home) else t.cwd
                pid_info = f" (PID: {t.pid})" if t.pid else ""
                print(f"  \033[90m↳ Tool     :\033[0m \033[1;37m{t.tool_name}\033[0m")
                print(f"  \033[90m↳ Directory:\033[0m \033[1;33m{cwd_disp}\033[0m")
                print(f"  \033[90m↳ Window   :\033[0m \033[1;32m{emulator.name}{pid_info}\033[0m")

            parts = []
            for tool, cnt in counts.items():
                cwd = tool_dirs.get(tool, self.project_dir)
                if cwd == self.project_dir:
                    pretty_cwd = os.path.basename(cwd)
                elif cwd.startswith(home):
                    pretty_cwd = "~" + cwd[len(home):]
                else:
                    pretty_cwd = cwd
                parts.append(f"{cnt}x {tool} ({pretty_cwd})")
            print(f"  \033[1;32m✓ Opened {len(spawned)} terminal window(s):\033[0m {', '.join(parts)}")

    def handle_spawn_command(self, arg_str: str = "") -> None:
        """Open worker terminal window(s) via /spawn [count] [tool] [dir] or interactive menu."""
        arg_clean = arg_str.strip()
        if not arg_clean:
            options = [
                ("1", "antigravity_cli (agy)"),
                ("2", "claude_code (claude)"),
                ("3", "opencode"),
                ("4", "bash / system shell")
            ]
            choice = select_from_menu("? Select tool to spawn terminal for:", options)
            if not choice:
                return
            tool_map = {"1": "antigravity_cli", "2": "claude_code", "3": "opencode", "4": "bash"}
            tool = tool_map.get(choice, "antigravity_cli")

            cnt_str = read_input_with_esc("Number of terminals to open [default: 1]: ")
            if cnt_str is None:
                return
            count = int(cnt_str.strip()) if cnt_str.strip().isdigit() else 1

            dir_str = read_input_with_esc(f"Working directory [default: {self.project_dir}]: ")
            if dir_str is None:
                return
            cwd = os.path.abspath(os.path.expanduser(dir_str.strip())) if dir_str.strip() else self.project_dir

            specs = [{"provider": tool, "count": count, "dir": cwd}]
            self.open_requested_terminals(specs)
            return

        spec = parse_spawn_args(arg_clean, default_provider=self._get_active_provider_name(), default_dir=self.project_dir)
        if spec:
            self.open_requested_terminals([spec])
        else:
            print("[!] Could not parse spawn arguments. Usage: /spawn [count] [tool] [dir]")

    def handle_system_status(self) -> None:
        """Display real-time System Resource Guard metrics."""
        from orchestrator.system_guard import SystemGuard
        print("\n" + SystemGuard.render_report(self.project_dir))

    def handle_new_session(self) -> None:
        """Start a fresh conversation thread with active AI worker."""
        from orchestrator.terminal import close_all_visual_sessions
        close_all_visual_sessions()
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
        """Inspect task status and contents interactively with arrow keys."""
        statuses = self.hub.get_all_task_statuses()
        if not statuses:
            print("\n[!] No tasks found in this project workspace yet.")
            return

        options = []
        for s in statuses:
            role_tag = f"[{s.role.upper()}]"
            state_tag = f"[{s.state.upper()}]"
            summary_preview = f"({s.one_line_summary[:28]}...)" if len(s.one_line_summary) > 30 else f"({s.one_line_summary})" if s.one_line_summary else ""
            label = f"{role_tag:<12} {s.title[:28]:<28} {state_tag:<12} {summary_preview}"
            options.append((s.task_id, label))

        pick = select_from_menu(f"? Select a task to inspect details & verdict ({len(statuses)} tasks):", options)
        if not pick:
            return

        spec = self.hub.read_task_spec(pick)
        if spec:
            print("\n" + render_markdown(spec))
            log_content = self.hub.read_task_log(pick)
            if log_content:
                lines_cnt = len(log_content.splitlines())
                print(f"\n\033[90m(Task log: hub/logs/{pick}.log — {lines_cnt} lines recorded)\033[0m")
        else:
            print(f"Task #{pick} specification not found.")

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
                f"* **Reasoning Effort**: `{self.effort.upper()}`\n"
                f"* **Direct AI Chat**: Ask questions, brainstorm, or explore codebase naturally.\n"
                f"* **Autonomous Sprint**: Run `/run <goal>` to dispatch a complete 4-agent sprint.\n"
                f"* **Menu & Tools**: Type `/` to open live dropdown (`/effort`, `/close`, `/provider`, `/status`, `/link`, `/doctor`)."
            )
            print("\n" + render_markdown(content))
            return True

        # Close active worker terminals (Instant local execution in < 0.05s)
        close_triggers = [
            "close terminals", "close all terminals", "close terminal", "close windows",
            "kill terminals", "exit terminals", "stop terminals", "shut terminals",
            "close the terminals", "close the terminal", "close all open terminals",
            "close the windows", "close all windows",
            "টার্মিনাল বন্ধ করো", "সব টার্মিনাল বন্ধ করো", "টার্মিনালগুলো বন্ধ করে দাও",
            "টার্মিনালগুলো বন্ধ করো", "টার্মিনাল বন্ধ", "উইন্ডো বন্ধ করো", "সব উইন্ডো বন্ধ করো"
        ]
        if any(tr in q for tr in close_triggers) or q in ("/close", "close"):
            self.handle_close_terminals()
            return True

        # Reasoning Effort Level
        if q in ("effort", "reasoning effort", "effort level", "change effort") or q.startswith("effort "):
            parts = q.split()
            arg = parts[1] if len(parts) > 1 else ""
            self.handle_effort_menu(arg)
            return True

        return False

    def bridge_to_ai_provider(self, query: str) -> None:
        """
        Send user query/instruction directly to active AI provider (agy, claude, opencode)
        with workspace context and live elapsed timer. All intelligence and tool execution
        resides with the AI provider.
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

        desktop_info = ""
        try:
            from orchestrator.terminal import detect_desktop_terminal
            term_emulator = detect_desktop_terminal()
            if term_emulator:
                desktop_info = f"\nDesktop Terminal Emulator: {term_emulator.name} ({term_emulator.executable})"
        except Exception:
            pass

        effort_instruction = {
            "low": "Reasoning Effort: LOW (Fast & Concise Mode). Prioritize brevity, speed, and execute directly with minimal overhead.",
            "medium": "Reasoning Effort: MEDIUM (Balanced Mode). Balanced depth, standard analysis, and clear execution.",
            "high": "Reasoning Effort: HIGH (Deep Reasoning Mode). Rigorously explore edge cases, verify all assumptions, and provide deep architectural precision."
        }.get(self.effort, "Reasoning Effort: MEDIUM.")

        if self.session_turns > 0:
            prompt = (
                f"[Workspace Update: {self.project_dir} | State: {state_content[:600]}]\n"
                f"{effort_instruction}\n\n"
                f"User Request: {query}\n\n"
                f"Instructions:\n"
                f"You are the intelligent AI agent for Foreman Orchestrator.\n"
                f"You have full command execution and filesystem tools available.\n"
                f"Respond directly, search/inspect files if needed, and execute any requested actions.\n"
                f"If the user asks to open/launch worker terminals in any directory, emit '/spawn <tool> <directory>' on its own line so Foreman automatically opens them."
            )
            session_arg = self.session_id if p_name == "claude_code" else "continue"
        else:
            prompt = (
                f"You are the Front-Desk AI Worker for Foreman Orchestrator.\n"
                f"Project Directory: {self.project_dir}\n"
                f"Connected Workspaces: {links_summary}\n"
                f"Current Project State:\n{state_content[:2000]}{desktop_info}\n\n"
                f"{effort_instruction}\n\n"
                f"User Request: {query}\n\n"
                f"Instructions:\n"
                f"1. You are an autonomous intelligent agent acting on behalf of the user. You have full command execution and tool access.\n"
                f"2. Answer the user directly, clearly, and concisely based on the project context.\n"
                f"3. If the user asks you to locate, search, inspect, or open projects, folders, or terminal sessions (e.g. for opencode, agy/antigravity, claude, or bash):\n"
                f"   - Search the filesystem to find the actual directory (e.g. using `find` or `ls` in `~`, `~/Desktop`, `~/projects`). Do not guess or fail if the user's wording is approximate or has typos.\n"
                f"   - To launch worker terminal windows on desktop, emit `/spawn <tool> <directory>` on its own line for each requested terminal so Foreman Orchestrator automatically launches them.\n"
                f"   - Do NOT tell the user to manually run bash commands; Foreman executes the spawn directives directly on their desktop.\n"
                f"   - Report back clearly to the user what you found and what actions you executed.\n"
                f"4. If the user is proposing a software feature or engineering goal, provide a crisp answer or outline, and inform them they can run '/run <goal>' to trigger a complete multi-agent sprint.\n"
                f"5. All intelligence resides with you; execute tools and report results directly."
            )
            session_arg = self.session_id if p_name == "claude_code" else None

        adapter = create_adapter(p_name, p_cfg, timeout_sec=self.config.budget.max_time_per_task_sec, effort=self.effort)
        adapter.visual_mode = self.visual_mode
        stream_mode = (not self.visual_mode)
        result = adapter.run(
            prompt=prompt,
            cwd=self.project_dir,
            task_id="front_desk",
            session_id=session_arg,
            show_progress=True,
            progress_label=p_name,
            stream_output=stream_mode
        )

        self.last_duration_sec = result.duration_sec

        if result.success and result.output:
            self.session_turns += 1
            if not stream_mode:
                clean_output = clean_user_facing_output(result.output)
                if clean_output:
                    formatted = render_markdown(clean_output)
                    print(formatted)

            # Check for spawn directives emitted by AI worker
            specs = extract_spawn_directives(
                result.output,
                default_provider=p_name,
                default_dir=self.project_dir
            )
            if specs:
                self.open_requested_terminals(specs)

        elif result.error:
            print_claude_error(result.error)
        elif not stream_mode:
            print("\n\033[90m(No response received from AI worker.)\033[0m\n")

        # Claude Code style execution completion footer
        if result.duration_sec > 0:
            print(f"\n\033[90m⏺ \033[1;36m{p_name}\033[0m \033[90m• effort: \033[1;34m{self.effort.upper()}\033[0m \033[90m• duration: \033[0m{result.duration_sec:.1f}s\033[0m")

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

        if os.path.isfile(goal.strip()):
            try:
                with open(goal.strip(), "r", encoding="utf-8") as f:
                    goal = f.read().strip()
            except Exception:
                pass

        if not self.check_or_prompt_ai_provider():
            return

        self.execute_goal(goal)

    def _render_status_bar(self) -> str:
        """Render a single-line status bar with provider, system health, active workers, and visual mode."""
        active_p = self._get_active_provider_name()
        try:
            from orchestrator.system_guard import SystemGuard
            metrics = SystemGuard.get_metrics(self.project_dir)
            ram_str = f"{metrics.ram_used_percent:.0f}%"
            cpu_str = f"{metrics.cpu_percent:.0f}%"
            health_color = "\033[1;32m" if metrics.is_healthy else "\033[1;33m"
        except Exception:
            ram_str = "N/A"
            cpu_str = "N/A"
            health_color = "\033[1;32m"

        vis_str = "\033[1;32mON\033[0m" if self.visual_mode else "\033[90mOFF\033[0m"
        try:
            from orchestrator.terminal import get_active_spawned_terminals
            active_cnt = len(get_active_spawned_terminals())
        except Exception:
            active_cnt = 0

        p_badge = f"\033[1;36mProvider:\033[0m {active_p}"
        e_badge = f"\033[1;34mEffort:\033[0m {self.effort.upper()}"
        last_badge = f" \033[90m──\033[0m [\033[1;33mLast:\033[0m {self.last_duration_sec:.1f}s]" if self.last_duration_sec is not None else ""
        r_badge = f"{health_color}RAM:\033[0m {ram_str}"
        c_badge = f"\033[90mCPU:\033[0m {cpu_str}"
        w_badge = f"\033[1;33mWorkers:\033[0m {active_cnt} active"
        v_badge = f"\033[1;35mVisual:\033[0m {vis_str}"

        return f"\033[90m╭─\033[0m [{p_badge}] \033[90m──\033[0m [{e_badge}] \033[90m──\033[0m [{r_badge}] \033[90m──\033[0m [{c_badge}] \033[90m──\033[0m [{w_badge}] \033[90m──\033[0m [{v_badge}]{last_badge}"

    def run(self) -> None:
        """Main interactive REPL loop."""
        def _sig_cleanup(signum, frame):
            print("\nExiting orchestrator. Cleaning up sessions...")
            from orchestrator.terminal import close_all_visual_sessions, close_all_spawned_terminals
            close_all_visual_sessions()
            close_all_spawned_terminals()
            sys.exit(0)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _sig_cleanup)
            except Exception:
                pass

        self.render_banner()

        try:
            while True:
                try:
                    status_line = self._render_status_bar()
                    print(f"\n{status_line}")
                    prompt = "\033[90m╰─\033[1;36m❯\033[0m "
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
                    all_cmds = [
                        "/run", "/effort", "/close", "/spawn", "/new", "/provider", "/visual", "/status",
                        "/system", "/tasks", "/link", "/doctor", "/clear", "/help", "/exit", "/quit"
                    ]
                    matches = [c for c in all_cmds if c.startswith(cmd)]
                    if len(matches) == 1:
                        cmd = matches[0]

                    if cmd in ("/exit", "/quit"):
                        print("Exiting orchestrator. Goodbye!")
                        break
                    elif cmd in ("/effort", "/thinking"):
                        self.handle_effort_menu(cmd_parts[1] if len(cmd_parts) > 1 else "")
                    elif cmd in ("/close", "/terminals", "/kill-terminals"):
                        self.handle_close_terminals()
                    elif cmd == "/run":
                        self.handle_run_command(arg)
                    elif cmd == "/spawn":
                        self.handle_spawn_command(arg)
                    elif cmd in ("/system", "/guard", "/hardware"):
                        self.handle_system_status()
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
                    # Forward user prompt directly to the active AI Provider session!
                    # The AI model processes, reasons, and executes the request directly.
                    self.bridge_to_ai_provider(line)
        finally:
            from orchestrator.terminal import close_all_visual_sessions, close_all_spawned_terminals
            close_all_visual_sessions()
            close_all_spawned_terminals()
