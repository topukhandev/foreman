"""Intelligent tool and CLI binary finder.
Auto-resolves executables across NVM, FNM, Volta, npm-global, system binaries,
and handles unconventional PATH formats across Linux, macOS, and Windows.
"""

import glob
import os
import shutil
import sys
from typing import Dict, List, Optional


KNOWN_ALIASES: Dict[str, List[str]] = {
    "antigravity_cli": ["agy", "antigravity"],
    "agy": ["agy", "antigravity"],
    "antigravity": ["antigravity", "agy"],
    "claude_code": ["claude", "claude-code"],
    "claude": ["claude", "claude-code"],
    "opencode": ["opencode", "opencode-cli", "open-code"],
    "opencode_cli": ["opencode-cli", "opencode", "open-code"],
    "git": ["git"]
}


def find_tool_executable(tool_or_cmd: str, alt_names: Optional[List[str]] = None) -> Optional[str]:
    """
    Intelligently discover full executable path for an AI CLI tool.
    Handles NVM, npm global prefixes, malformed PATH entries, and aliases.
    """
    clean_input = tool_or_cmd.strip()
    if clean_input.lower() == "mock":
        return "mock"

    parts = clean_input.split()
    primary_name = parts[0] if parts else clean_input

    # If already an absolute or relative path that exists
    if os.path.isfile(primary_name) and os.access(primary_name, os.X_OK):
        return os.path.abspath(primary_name)

    # Build candidate search list
    candidates: List[str] = [primary_name]
    if alt_names:
        candidates.extend(alt_names)
    if primary_name in KNOWN_ALIASES:
        candidates.extend(KNOWN_ALIASES[primary_name])
    # Deduplicate while preserving order
    candidates = list(dict.fromkeys(candidates))

    # 1. Standard shutil.which
    for cand in candidates:
        found = shutil.which(cand)
        if found and os.path.isfile(found) and os.access(found, os.X_OK):
            return os.path.abspath(found)

    # 2. Inspect PATH entries directly (handles when a user put the file path in PATH instead of dir)
    raw_path = os.environ.get("PATH", "")
    path_entries = [p.strip() for p in raw_path.split(os.pathsep) if p.strip()]

    for entry in path_entries:
        # Check if entry is directly the executable file
        if os.path.isfile(entry) and os.access(entry, os.X_OK):
            base = os.path.basename(entry)
            for cand in candidates:
                if base == cand or base.startswith(cand):
                    # Found direct executable file in PATH!
                    # Also ensure its parent folder is properly in PATH
                    _ensure_in_path(os.path.dirname(entry))
                    return os.path.abspath(entry)

        # Check if entry is a file, check its directory
        target_dir = os.path.dirname(entry) if os.path.isfile(entry) else entry
        if os.path.isdir(target_dir):
            for cand in candidates:
                cand_file = os.path.join(target_dir, cand)
                if os.path.isfile(cand_file) and os.access(cand_file, os.X_OK):
                    _ensure_in_path(target_dir)
                    return os.path.abspath(cand_file)

    # 3. Search common node version managers & user package locations
    home = os.path.expanduser("~")
    search_patterns = [
        # NVM
        os.path.join(home, ".nvm/versions/node/*/bin/*"),
        # FNM
        os.path.join(home, ".local/share/fnm/current/bin/*"),
        os.path.join(home, ".fnm/current/bin/*"),
        # Volta
        os.path.join(home, ".volta/bin/*"),
        # NPM Global
        os.path.join(home, ".npm-global/bin/*"),
        os.path.join(home, ".npm/bin/*"),
        # Yarn
        os.path.join(home, ".yarn/bin/*"),
        # Local user bin
        os.path.join(home, ".local/bin/*"),
        # Standard system locations
        "/usr/local/bin/*",
        "/usr/bin/*",
        "/bin/*",
        "/snap/bin/*"
    ]

    # Windows locations if on Windows
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        localappdata = os.environ.get("LOCALAPPDATA", "")
        if appdata:
            search_patterns.append(os.path.join(appdata, "npm", "*"))
        if localappdata:
            search_patterns.append(os.path.join(localappdata, "Programs", "*", "bin", "*"))

    for pattern in search_patterns:
        for match in glob.glob(pattern):
            if os.path.isfile(match) and os.access(match, os.X_OK):
                base_name = os.path.basename(match)
                # Strip .exe or .cmd on Windows
                if sys.platform == "win32":
                    base_name = os.path.splitext(base_name)[0]
                for cand in candidates:
                    if base_name == cand:
                        target_dir = os.path.dirname(match)
                        _ensure_in_path(target_dir)
                        return os.path.abspath(match)

    return None


def _ensure_in_path(directory: str) -> None:
    """Ensure directory is present in os.environ['PATH'] so child subprocesses can execute it."""
    if not directory or not os.path.isdir(directory):
        return
    current_path = os.environ.get("PATH", "")
    parts = current_path.split(os.pathsep)
    if directory not in parts:
        os.environ["PATH"] = f"{directory}{os.pathsep}{current_path}"
