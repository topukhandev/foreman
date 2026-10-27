"""The roles workers take (deterministic). A step's kind decides its role; permissions live here and in the mode
(modes.py), never in the guidance files: those are plain text (built-in roles/*.md, replaced by a file with the same
name in ~/.foreman/roles or <project>/.foreman/roles) and can't grant anything.

Adding a role later (an architect, a security reviewer, ...) means a new step kind, an entry in ROLES, a guidance file
and, if its result differs, a schema in brain.py; the engine's scheduling, sessions and verification stay as they are.
"""

import os
from dataclasses import dataclass
from typing import Dict, Optional

from orchestrator.util import read_text

BUILTIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "roles")


@dataclass(frozen=True)
class Role:
    name: str
    title: str
    writes: str  # the most this role may change: "code" or "none" (the mode can only narrow it further)


ROLES: Dict[str, Role] = {
    "implement": Role("developer", "Developer", "code"),
    "investigate": Role("researcher", "Researcher", "none"),
    "verify": Role("qa", "QA verifier", "none"),
}


def role_of(kind: str) -> Role:
    return ROLES.get(kind) or ROLES["investigate"]  # an unknown kind gets the read-only role


def effective_writes(kind: str, mode_writes: Optional[str]) -> str:
    """What a step may change: the role's limit narrowed by the mode (none < tests < code)."""
    order = ["none", "tests", "code"]
    role = role_of(kind).writes
    mode = mode_writes if mode_writes in order else "code"
    return order[min(order.index(role), order.index(mode))]


def guidance(role: Role, home: Optional[str] = None, project_path: Optional[str] = None) -> str:
    """The role's guidance text: the project's file, else yours, else the built-in one."""
    for folder in ([os.path.join(project_path, ".foreman", "roles")] if project_path else []) + \
            ([os.path.join(home, "roles")] if home else []) + [BUILTIN_DIR]:
        text = read_text(os.path.join(folder, f"{role.name}.md")).strip()
        if text:
            return text
    return ""
