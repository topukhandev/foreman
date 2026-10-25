"""Lifecycle modes (R&D, Planning, Development, QA, Deployment) as plain template files.

Each mode is a markdown file with a small YAML header (what workers may write, whether web research is allowed,
which checks Foreman runs, where the result document is saved, the usual next mode) and sections of guidance
that are handed to whichever provider plans or executes the work. Built-in templates live in ./modes/; a file with
the same name in ~/.foreman/modes/ (personal) or <project>/.foreman/modes/ (shared with the team through
git) replaces it.
"""

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import yaml

from orchestrator.util import read_text

BUILTIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "modes")
ORDER = ["research", "planning", "development", "qa", "deployment"]
WRITES = ("none", "code", "tests")
ALIASES = {"r&d": "research", "rnd": "research", "r-and-d": "research", "plan": "planning", "dev": "development",
           "develop": "development", "build": "development", "test": "qa", "testing": "qa", "deploy": "deployment",
           "release": "deployment"}


@dataclass
class Mode:
    name: str
    title: str
    summary: str
    writes: str = "none"            # none | code | tests
    web: bool = False
    checks: List[str] = field(default_factory=list)
    document: Optional[str] = None  # .foreman/<document>/<feature>.md
    next: Optional[str] = None
    sections: Dict[str, str] = field(default_factory=dict)  # when / planner / worker / output
    source: str = "built-in"

    def section(self, name: str) -> str:
        return self.sections.get(name, "")


def parse(text: str, name: str, source: str = "built-in") -> Mode:
    meta, body = {}, text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
    if m:
        meta, body = yaml.safe_load(m.group(1)) or {}, m.group(2)
    sections: Dict[str, str] = {}
    for part in re.split(r"(?m)^##\s+", body)[1:]:
        head, _, content = part.partition("\n")
        sections[head.strip().lower()] = content.strip()
    writes = str(meta.get("writes") or "none").lower()
    return Mode(name=name, title=str(meta.get("title") or name.title()), summary=str(meta.get("summary") or ""),
                writes=writes if writes in WRITES else "none", web=bool(meta.get("web", False)),
                checks=[str(c) for c in meta.get("checks") or []],
                document=str(meta["document"]) if meta.get("document") else None,
                next=str(meta["next"]) if meta.get("next") else None, sections=sections, source=source)


def load_modes(home: Optional[str] = None, project_path: Optional[str] = None) -> Dict[str, Mode]:
    """Built-in modes, overridden by ~/.foreman/modes and then by <project>/.foreman/modes (same file name)."""
    modes: Dict[str, Mode] = {}
    dirs = [(BUILTIN_DIR, "built-in")]
    if home:
        dirs.append((os.path.join(home, "modes"), "~/.foreman/modes"))
    if project_path:
        dirs.append((os.path.join(project_path, ".foreman", "modes"), ".foreman/modes"))
    for d, source in dirs:
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.endswith(".md"):
                name = f[:-3].lower()
                modes[name] = parse(read_text(os.path.join(d, f)), name, source)
    ordered = {n: modes[n] for n in ORDER if n in modes}
    ordered.update({n: m for n, m in modes.items() if n not in ordered})  # custom modes after the built-in ones
    return ordered


def resolve(name: Optional[str], modes: Dict[str, Mode]) -> Optional[str]:
    """Mode name from user input ('QA', 'r&d', 'deploy'), or None for auto/unknown."""
    if not name:
        return None
    key = name.strip().lower()
    key = ALIASES.get(key, key)
    if key in modes:
        return key
    for n, m in modes.items():
        if m.title.lower() == key:
            return n
    return None


def catalog(modes: Dict[str, Mode], only: Optional[str] = None) -> str:
    """Mode guidance for the planner: every mode (auto selection) or just the one the user chose."""
    out = []
    for n, m in modes.items():
        if only and n != only:
            continue
        rules = {"none": "read-only", "code": "workers may change code", "tests": "workers may change TEST files only"}
        out.append(f"### {n} ({m.title}): {m.summary}\n"
                   f"[{rules[m.writes]}; web research {'allowed' if m.web else 'not allowed'}"
                   + ("; result saved as a document" if m.document else "") + "]\n"
                   + (f"When: {m.section('when')}\n" if m.section("when") and not only else "")
                   + m.section("planner")
                   + (f"\nThe final result must be: {m.section('output')}" if m.section("output") else ""))
    return "\n\n".join(out)
