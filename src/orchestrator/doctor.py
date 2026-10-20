"""Setup Doctor: Diagnoses environment, CLI tools, and produces plain-language setup instructions."""

import platform
import shutil
import sys
from typing import Any, Dict, List, Optional
from orchestrator.config import OrchestratorConfig, detect_os
from orchestrator.finder import find_tool_executable


class SetupDoctor:
    """Diagnoses CLI worker tools and offers exact OS-specific fix commands."""

    INSTALL_COMMANDS = {
        "antigravity_cli": {
            "linux": "curl -fsSL https://antigravity.google/install.sh | bash",
            "darwin": "brew install antigravity-cli || curl -fsSL https://antigravity.google/install.sh | bash",
            "windows": "winget install Google.Antigravity || irm https://antigravity.google/install.ps1 | iex",
            "reason": "Antigravity CLI provides planner, coder, and reviewer sessions."
        },
        "claude_code": {
            "linux": "npm install -g @anthropic-ai/claude-code && claude login",
            "darwin": "npm install -g @anthropic-ai/claude-code && claude login",
            "windows": "npm install -g @anthropic-ai/claude-code; claude login",
            "reason": "Claude Code allows an independent model to review and critique tasks."
        },
        "opencode": {
            "linux": "npm install -g opencode-ai",
            "darwin": "brew install opencode || npm install -g opencode-ai",
            "windows": "npm install -g opencode-ai",
            "reason": "OpenCode provides free/local bulk execution and web research."
        },
        "git": {
            "linux": "sudo apt install git || sudo yum install git",
            "darwin": "xcode-select --install || brew install git",
            "windows": "winget install Git.Git",
            "reason": "Git allows isolated worker branches and rollback tracking."
        }
    }

    @classmethod
    def check_system(cls, config: Optional[OrchestratorConfig] = None) -> Dict[str, Any]:
        """Run system diagnosis and report available tools and recommendations."""
        current_os = detect_os()
        python_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

        tools_status = {}
        for tool, bin_name in [
            ("antigravity_cli", "agy"),
            ("claude_code", "claude"),
            ("opencode", "opencode"),
            ("git", "git")
        ]:
            found_path = find_tool_executable(tool) or find_tool_executable(bin_name)

            tools_status[tool] = {
                "installed": bool(found_path),
                "path": found_path or None,
                "command_key": bin_name,
                "fix_command": cls.INSTALL_COMMANDS.get(tool, {}).get(current_os, ""),
                "reason": cls.INSTALL_COMMANDS.get(tool, {}).get("reason", "")
            }

        # Check configured providers status
        configured_summary = []
        if config:
            for p_name, p in config.providers.items():
                if p.enabled:
                    configured_summary.append({
                        "name": p_name,
                        "status": "ready" if p.verified else "error",
                        "error": p.verification_error,
                        "command": p.command,
                        "roles": p.roles
                    })

        return {
            "os": current_os,
            "python_version": python_ver,
            "tools": tools_status,
            "configured_providers": configured_summary
        }

    @classmethod
    def render_report(cls, config: Optional[OrchestratorConfig] = None) -> str:
        """Render a plain-language diagnosis report."""
        diag = cls.check_system(config)
        os_name = diag["os"].upper()
        py_ver = diag["python_version"]

        lines = [
            "===========================================================",
            f"          ORCHESTRATOR SETUP DOCTOR ({os_name})",
            "===========================================================",
            f" Python: {py_ver} [OK]",
            ""
        ]

        active_workers = 0
        missing_recommendations = []

        for tool_name, info in diag["tools"].items():
            if info["installed"]:
                active_workers += 1
                lines.append(f"  [x] {tool_name:<16} : FOUND ({info['path']})")
            else:
                lines.append(f"  [ ] {tool_name:<16} : NOT FOUND")
                if tool_name != "opencode":  # opencode is optional free tier
                    missing_recommendations.append((tool_name, info["fix_command"], info["reason"]))

        lines.append("")
        if active_workers == 0:
            lines.append("  [!] WARNING: No AI CLI worker found on this machine.")
            lines.append("      Orchestrator can run in mock/simulation mode or install a CLI.")
        else:
            lines.append(f"  [*] {active_workers} local tool(s) available for worker routing.")

        if missing_recommendations:
            lines.append("")
            lines.append("  Recommendations to enhance your setup:")
            for tool, cmd, reason in missing_recommendations:
                lines.append(f"  - {tool}: {reason}")
                lines.append(f"    Command: {cmd}")

        lines.append("===========================================================")
        return "\n".join(lines)
