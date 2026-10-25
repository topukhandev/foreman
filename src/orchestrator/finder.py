"""Locate CLI binaries, including version-manager and per-user install locations missing from PATH."""

import glob
import os
import shutil
from typing import Optional

_USER_BIN_GLOBS = [
    "~/.local/bin",
    "~/.npm-global/bin",
    "~/.volta/bin",
    "~/.bun/bin",
    "~/.opencode/bin",
    "~/.nvm/versions/node/*/bin",
    "~/.local/share/fnm/aliases/default/bin",
    "/usr/local/bin",
    "/opt/homebrew/bin",
]


def find_executable(name: str) -> Optional[str]:
    """Exact-name lookup only. Never substitutes a different program (e.g. the Antigravity IDE for `agy`)."""
    if os.path.sep in name:
        return name if os.path.isfile(name) and os.access(name, os.X_OK) else None
    found = shutil.which(name)
    if found:
        return found
    for pattern in _USER_BIN_GLOBS:
        for d in sorted(glob.glob(os.path.expanduser(pattern)), reverse=True):
            cand = os.path.join(d, name)
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
    return None
