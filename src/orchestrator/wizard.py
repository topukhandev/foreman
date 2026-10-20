"""Interactive Setup Wizard for configuring Orchestrator providers and budget presets."""

import os
import shutil
import socket
from typing import Any, Dict, Optional
import yaml
from orchestrator.config import detect_os
from orchestrator.doctor import SetupDoctor


PRESETS = {
    "1": {
        "name": "auto",
        "description": "Auto-Detect: Automatically enable all verified CLI tools on this PC."
    },
    "2": {
        "name": "antigravity_only",
        "description": "Antigravity Only: Uses local agy / antigravity CLI for all roles with fresh sessions."
    },
    "3": {
        "name": "claude_only",
        "description": "Claude Code Only: Uses claude -p for all roles."
    },
    "4": {
        "name": "dual_model",
        "description": "Dual Model (Antigravity + Claude): Builder and Reviewer run on separate models."
    },
    "5": {
        "name": "free_only",
        "description": "Free / OpenCode Only: Uses local or free CLI workers."
    },
    "6": {
        "name": "mock",
        "description": "Simulation / Mock Only: Offline testing with zero API tokens or costs."
    }
}


def build_config_for_preset(preset_key: str, detected_tools: Dict[str, Any]) -> Dict[str, Any]:
    """Build a configuration dictionary based on preset selection and detected tools."""
    hostname = socket.gethostname() or "local-pc"
    current_os = detect_os()

    has_agy = detected_tools.get("antigravity_cli", {}).get("installed", False)
    has_claude = detected_tools.get("claude_code", {}).get("installed", False)
    has_opencode = detected_tools.get("opencode", {}).get("installed", False)

    # Defaults
    enable_agy = False
    enable_claude = False
    enable_opencode = False
    enable_mock = False

    if preset_key == "1" or preset_key == "auto":
        enable_agy = has_agy
        enable_claude = has_claude
        enable_opencode = has_opencode
        if not (enable_agy or enable_claude or enable_opencode):
            enable_mock = True
    elif preset_key == "2" or preset_key == "antigravity_only":
        enable_agy = True
    elif preset_key == "3" or preset_key == "claude_only":
        enable_claude = True
    elif preset_key == "4" or preset_key == "dual_model":
        enable_agy = True
        enable_claude = True
    elif preset_key == "5" or preset_key == "free_only":
        enable_opencode = True
    elif preset_key == "6" or preset_key == "mock":
        enable_mock = True

    return {
        "machine": hostname,
        "os": current_os,
        "providers": {
            "antigravity_cli": {
                "enabled": enable_agy,
                "tier": "paid",
                "command": "agy -p",
                "roles": ["planner", "critic", "coder", "researcher", "reviewer"],
                "limits": {"max_runs_per_day": 50, "max_parallel": 2}
            },
            "claude_code": {
                "enabled": enable_claude,
                "tier": "paid",
                "command": "claude -p",
                "roles": ["planner", "critic", "coder", "reviewer"],
                "limits": {"max_runs_per_day": 50, "max_parallel": 2}
            },
            "opencode": {
                "enabled": enable_opencode,
                "tier": "free",
                "command": "opencode run",
                "roles": ["bulk", "researcher"],
                "limits": {"max_runs_per_day": 100, "max_parallel": 4}
            },
            "mock": {
                "enabled": enable_mock,
                "tier": "free",
                "command": "mock",
                "roles": ["planner", "critic", "coder", "researcher", "reviewer"],
                "limits": {"max_runs_per_day": 1000, "max_parallel": 4}
            }
        },
        "routing": {
            "reviewer_must_differ_from_builder": True,
            "fallback": "any_enabled"
        },
        "budget": {
            "max_parallel": 2,
            "max_time_per_task_sec": 300,
            "max_retries": 3,
            "warn_at_percent": 80,
            "loop_detect_threshold": 3,
            "max_files_per_project": 1000,
            "max_project_size_mb": 500
        }
    }


def run_setup_wizard(project_dir: str, preset: Optional[str] = None, non_interactive: bool = False) -> str:
    """Run interactive or automated first-run setup wizard."""
    os.makedirs(project_dir, exist_ok=True)
    config_path = os.path.join(project_dir, "providers.yaml")

    diag = SetupDoctor.check_system()
    tools = diag["tools"]

    print("===========================================================")
    print("           ORCHESTRATOR FIRST-RUN SETUP WIZARD")
    print("===========================================================")
    print(f" Detected OS: {diag['os'].upper()} (Python {diag['python_version']})")
    print(" Installed Tools:")
    for t_name, t_info in tools.items():
        status_sym = "[x]" if t_info["installed"] else "[ ]"
        loc = f"({t_info['path']})" if t_info["installed"] else "Not installed"
        print(f"   {status_sym} {t_name:<16}: {loc}")
    print("-----------------------------------------------------------")

    selected_preset = preset or "1"

    if not non_interactive and not preset:
        print("\nChoose a configuration preset:")
        for k, p in PRESETS.items():
            print(f"  [{k}] {p['name']:<18} - {p['description']}")

        try:
            choice = input("\nEnter choice [default: 1]: ").strip()
            if choice in PRESETS:
                selected_preset = choice
        except (EOFError, KeyboardInterrupt):
            selected_preset = "1"

    cfg_dict = build_config_for_preset(selected_preset, tools)

    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg_dict, f, sort_keys=False)

    print(f"\n[OK] Configuration generated and saved to: {config_path}")
    enabled_list = [
        name for name, p in cfg_dict["providers"].items() if p.get("enabled")
    ]
    print(f"[*] Active Enabled Providers: {', '.join(enabled_list) or 'None'}")
    print("===========================================================")
    return config_path
