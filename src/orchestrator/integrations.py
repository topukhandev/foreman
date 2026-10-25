"""Register the Foreman MCP server with front-end tools (Claude Code, Antigravity app/CLI, Codex)."""

import subprocess
from typing import Dict, List

from orchestrator.app import entry_command
from orchestrator.finder import find_executable

SERVER_NAME = "foreman"
LEGACY_NAME = "switchyard"  # registered while the project was called Switchyard; replaced on install


def _run(cmd: List[str]) -> str:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"failed: {e}"
    return "ok" if res.returncode == 0 else f"failed: {(res.stderr or res.stdout).strip()[-300:]}"


def install(targets: List[str]) -> Dict[str, str]:
    launch = entry_command() + ["mcp"]
    results: Dict[str, str] = {}
    for t in targets:
        if t == "claude":
            exe = find_executable("claude")
            if not exe:
                results[t] = "skipped: claude not installed"
                continue
            for name in (SERVER_NAME, LEGACY_NAME):
                _run([exe, "mcp", "remove", "--scope", "user", name])
            results[t] = _run([exe, "mcp", "add", "--scope", "user", SERVER_NAME, "--", *launch])
        elif t == "antigravity":
            exe = find_executable("agy")
            if not exe:
                results[t] = "skipped: agy not installed (the Antigravity app and agy share ~/.gemini/antigravity/mcp_config.json)"
                continue
            _run([exe, "mcp", "remove", LEGACY_NAME])
            results[t] = _run([exe, "mcp", "add", SERVER_NAME, "--", *launch])
        elif t == "codex":
            exe = find_executable("codex")
            if not exe:
                results[t] = ("skipped: codex not installed. After installing run: codex mcp add "
                              f"{SERVER_NAME} -- {' '.join(launch)}")
                continue
            results[t] = _run([exe, "mcp", "add", SERVER_NAME, "--", *launch])
        else:
            results[t] = "unknown target (use claude, antigravity, codex)"
    return results


def status() -> Dict[str, str]:
    out = {}
    for name, cmd in (("claude", ["claude", "mcp", "list"]), ("antigravity", ["agy", "mcp", "list"]),
                      ("codex", ["codex", "mcp", "list"])):
        exe = find_executable(cmd[0])
        if not exe:
            out[name] = "not installed"
            continue
        try:
            res = subprocess.run([exe, *cmd[1:]], capture_output=True, text=True, timeout=60)
            out[name] = "registered" if SERVER_NAME in res.stdout else "not registered"
        except (OSError, subprocess.TimeoutExpired):
            out[name] = "unknown"
    return out

