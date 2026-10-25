"""Project knowledge kept inside each repository, in `.foreman/` (commit it to share it with your team).

    .foreman/decisions.md     business decisions the user confirmed (answers to questions); fed to every plan/step
    .foreman/lifecycle.yaml   features and where each stands: last mode, outcome, recommended next step
    .foreman/knowledge.md     optional notes written by people (glossary, business rules, conventions)
    .foreman/checks.yaml      optional check commands by kind (test, lint, typecheck, build, e2e)
    .foreman/modes/*.md       optional mode templates overriding the built-in ones
    .foreman/plans|research|qa|releases/*.md   documents produced by those modes

Foreman writes here itself (workers never do), and its change detection ignores this folder.
"""

import os
import re
from typing import Any, Dict, List, Optional

import yaml

from orchestrator.util import atomic_write, now_iso, read_text, short

DIR = ".foreman"
README = """\
# .foreman

Project knowledge maintained by Foreman (the AI orchestrator) and by you:

- `decisions.md`: business decisions you confirmed; every AI session working here is given them.
- `lifecycle.yaml`: features and their stage (R&D, planning, development, QA, deployment) with the next step.
- `plans/`, `research/`, `qa/`, `releases/`: documents produced in those modes.
- `knowledge.md` (optional, yours): glossary, business rules, conventions the AI should know.
- `checks.yaml` (optional): check commands by kind, e.g. `lint: npm run lint`.

Commit this folder to share it with your team. Edit freely; Foreman only appends and updates.
"""
DECISIONS_HEADER = ("# Decisions\n\nBusiness decisions confirmed by the user, recorded by Foreman. Planners and "
                    "workers follow them. Edit or remove lines freely.\n\n")
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursorrules", ".github/copilot-instructions.md")
CHECK_KINDS = ("test", "lint", "typecheck", "build", "e2e")


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:50].rstrip("-") or "general"


class ProjectKnowledge:
    def __init__(self, project_path: str):
        self.root = project_path
        self.dir = os.path.join(project_path, DIR)

    def _path(self, *parts: str) -> str:
        return os.path.join(self.dir, *parts)

    def _ensure(self) -> None:
        os.makedirs(self.dir, exist_ok=True)
        if not os.path.exists(self._path("README.md")):
            atomic_write(self._path("README.md"), README)

    # ------------------------------------------------------------------ decisions
    def decisions(self, limit: int = 20) -> List[str]:
        lines = [l[2:].strip() for l in read_text(self._path("decisions.md")).splitlines() if l.startswith("- ")]
        return lines[-limit:]

    def add_decision(self, question: str, answer: str, task_id: str = "", feature: str = "") -> None:
        self._ensure()
        path = self._path("decisions.md")
        text = read_text(path) or DECISIONS_HEADER
        q = " ".join(question.split())
        a = " ".join(answer.split())
        entry = f"- {now_iso()[:10]}" + (f" · {feature}" if feature else "") + f" · {q} → {a}" + \
                (f" (task #{task_id})" if task_id else "")
        if f"{q} → {a}" in text:
            return
        atomic_write(path, text.rstrip("\n") + "\n" + entry + "\n")

    # ------------------------------------------------------------------ lifecycle
    def lifecycle(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = yaml.safe_load(read_text(self._path("lifecycle.yaml"))) or {}
        except yaml.YAMLError:
            return {}
        feats = data.get("features") if isinstance(data, dict) else None
        return feats if isinstance(feats, dict) else {}

    def update_feature(self, feature: str, title: str, stage: str, status: str, task_id: str, summary: str,
                       next_mode: Optional[str], next_reason: str, document: Optional[str] = None) -> None:
        self._ensure()
        feats = self.lifecycle()
        key = slug(feature)
        f = feats.get(key) or {"title": title}
        f.update({"title": f.get("title") or title, "stage": stage, "status": status, "updated": now_iso(),
                  "task": task_id, "summary": short(summary, 300), "next": next_mode, "next_reason": next_reason})
        if document:
            f["documents"] = list(dict.fromkeys((f.get("documents") or []) + [document]))[-8:]
        f["history"] = ((f.get("history") or []) + [{"mode": stage, "status": status, "task": task_id,
                                                      "at": now_iso()}])[-12:]
        feats[key] = f
        atomic_write(self._path("lifecycle.yaml"),
                     "# Maintained by Foreman: features, the stage each reached and the recommended next step.\n"
                     + yaml.safe_dump({"features": feats}, sort_keys=False, allow_unicode=True))

    # ------------------------------------------------------------------ documents, notes, checks
    def save_document(self, kind: str, feature: str, text: str, task_id: str) -> str:
        """Write a mode's result document; never overwrites (a new version gets a suffix). Returns the repo path."""
        self._ensure()
        base = slug(feature)
        os.makedirs(self._path(kind), exist_ok=True)
        name, n = f"{base}.md", 2
        while os.path.exists(self._path(kind, name)):
            name, n = f"{base}-v{n}.md", n + 1
        atomic_write(self._path(kind, name), f"<!-- Foreman · task #{task_id} · {now_iso()[:16]} -->\n\n"
                     + text.strip() + "\n")
        return f"{DIR}/{kind}/{name}"

    def notes(self, limit: int = 2500) -> str:
        return read_text(self._path("knowledge.md"), limit).strip()

    def checks(self) -> Dict[str, str]:
        try:
            data = yaml.safe_load(read_text(self._path("checks.yaml"))) or {}
        except yaml.YAMLError:
            return {}
        return {str(k): str(v) for k, v in data.items() if k in CHECK_KINDS and v} if isinstance(data, dict) else {}

    def instruction_files(self) -> List[str]:
        return [f for f in INSTRUCTION_FILES if os.path.isfile(os.path.join(self.root, f))]

    def context(self, name: str, decisions: int = 15) -> str:
        """Compact knowledge block for prompts: lifecycle, decisions, notes and instruction files."""
        parts = []
        feats = self.lifecycle()
        if feats:
            parts.append("  Lifecycle: " + "; ".join(
                f"{k} = {v.get('stage')} ({v.get('status')})" + (f", next {v['next']}" if v.get("next") else "")
                for k, v in list(feats.items())[-8:]))
        dec = self.decisions(decisions)
        if dec:
            parts.append("  Confirmed business decisions (follow them):\n" + "\n".join(f"    - {d}" for d in dec))
        notes = self.notes(1200)
        if notes:
            parts.append("  Team notes (.foreman/knowledge.md):\n    " + notes.replace("\n", "\n    "))
        files = self.instruction_files()
        if files:
            parts.append(f"  Instruction files (read when relevant): {', '.join(files)}")
        return f"- {name}:\n" + "\n".join(parts) if parts else ""
