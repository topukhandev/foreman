"""AI reasoning used by the orchestrator itself: planning (mode, scope, steps), re-planning, independent review, and
turning a mode's findings into its document (research brief, plan, QA report, release document).

Everything here returns validated JSON. The brain runs read-only; it may look at code to settle scope,
but is told to stop as soon as scope is clear (progressive context expansion).
"""

import json
from typing import Any, Callable, Dict, List, Optional

from orchestrator.providers.base import Provider, RunRequest, RunResult

_STR = {"type": "string"}
_STRS = {"type": "array", "items": _STR}
SUGGESTIONS = {"type": "array", "items": {"type": "object", "properties": {
    "text": _STR, "why": _STR, "project": _STR, "impact": {"type": "string", "enum": ["major", "minor"]}},
    "required": ["text", "why", "impact"]}}
SUGGESTION_RULE = ("Suggestions are for the user's decision: only MAJOR items outside the requested scope that they "
                   "probably forgot and that matter now (missing migration or deploy config, another consumer that "
                   "will break, missing coverage for a changed contract, a security or data-loss risk). Mark each "
                   "impact major or minor; minor ones are discarded, so put small in-scope improvements into the work "
                   "itself instead. Usually return none.")
QUESTIONS = {"type": "array", "items": {"type": "object", "properties": {
    "text": _STR, "kind": {"type": "string", "enum": ["business", "technical"]},
    "options": _STRS, "default": _STR}, "required": ["text", "kind"]}}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["answer", "change"]},
        "mode": _STR,
        "mode_reason": _STR,
        "feature": _STR,
        "title": _STR,
        "answer": _STR,
        "projects": {"type": "array", "items": {"type": "object", "properties": {
            "name": _STR, "role": {"type": "string", "enum": ["modify", "inspect", "test"]}, "why": _STR},
            "required": ["name", "role", "why"]}},
        "steps": {"type": "array", "items": {"type": "object", "properties": {
            "id": _STR, "project": _STR, "kind": {"type": "string", "enum": ["investigate", "implement"]},
            "instructions": _STR, "depends_on": _STRS, "read_projects": _STRS,
            "expected_minutes": {"type": "integer"}},
            "required": ["id", "project", "kind", "instructions"]}},
        "checks": {"type": "array", "items": {"type": "object", "properties": {
            "project": _STR, "command": _STR, "why": _STR}, "required": ["project", "command"]}},
        "questions": QUESTIONS,
        "decisions": _STRS,
        "suggestions": SUGGESTIONS,
        "impact": {"type": "object", "properties": {
            "summary": _STR, "risk": {"type": "string", "enum": ["low", "medium", "high"]},
            "consumers": _STRS, "tests": _STRS, "business_rules": _STRS, "interface_change": {"type": "boolean"}},
            "required": ["summary", "risk"]},
    },
    "required": ["intent", "title", "projects", "steps", "questions"],
}

DOC_SCHEMA = {
    "type": "object",
    "properties": {"title": _STR, "document": _STR, "summary": _STR, "open_problems": _STRS},
    "required": ["title", "document", "summary"],
}

STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["done", "blocked", "needs_input"]},
        "summary": _STR,
        "files_changed": _STRS,
        "findings": _STRS,
        "dependencies": {"type": "array", "items": {"type": "object", "properties": {
            "project": _STR, "type": _STR, "evidence": _STR}, "required": ["project", "type", "evidence"]}},
        "questions": QUESTIONS,
        "suggestions": SUGGESTIONS,
    },
    "required": ["status", "summary"],
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "revise"]},
        "summary": _STR,
        "issues": {"type": "array", "items": {"type": "object", "properties": {
            "project": _STR, "problem": _STR, "fix": _STR}, "required": ["project", "problem"]}},
        "suggestions": SUGGESTIONS,
    },
    "required": ["verdict", "summary", "issues"],
}

PLANNING_RULES = """\
Speed matters. If the request is conversational or answerable from the context above, answer right away in
"answer" without using any tools. Never read Foreman's own files (~/.foreman) or this prompt's sources.

How to plan (be proportional - most requests are small):
1. Scale effort to the request. Status, history, or explanation questions -> intent "answer". If the registry,
   task history, or a quick look at a few files lets you answer confidently, put the answer in "answer" and
   return no steps. Otherwise add ONE investigate step in the right project.
2. Progressive context: you have read-only tools. Open code only to settle scope (e.g. whether a changed API
   has consumers). Stop as soon as scope is clear. Never survey projects the request does not plausibly touch.
3. Include a project only with a concrete reason: it is named, it is the current directory, or a relation makes
   it plausibly affected. Relations marked "re-verify" may be outdated - verify with an investigate step or a
   check rather than assuming.
4. Unmentioned dependencies: if a change alters an interface (API route, payload, DB schema, shared type,
   contract, event, config key), include its known consumers and test projects (role inspect/test), even if
   the user did not mention them. Do not include them for internal-only changes.
5. Steps: one implement step per project to change, with precise instructions including which interface must
   stay consistent with which project. Use depends_on only for real ordering (e.g. backend contract before the
   frontend that consumes it). Independent steps run in parallel. read_projects lists other projects the worker
   may need to read. For a step that waits on long jobs (large downloads, builds, big scans) set expected_minutes
   honestly (Foreman allows about 1.5x that; a step that still runs out of time is resumed in the same session),
   and prefer splitting "start the job in the background" from "process its result".
6. Checks: commands Foreman runs itself to validate (tests, type-check, build) in modified or test projects.
   Prefer the registry's test command; narrow it when the suite is large. Non-destructive only: no deploys, DB
   resets, network installs or git commands.
7. Questions: only when a business/product rule cannot be inferred from code, docs or conventions and a wrong
   guess would be costly (kind "business"). Make technical decisions yourself and list them in "decisions".
8. Suggestions: at most 2. See the suggestion rule below; small in-scope fixes belong in the steps.
9. Impact (whenever steps change code): what existing behaviour, consumers, tests and confirmed business
   decisions the change touches, and the risk: low = local and covered by tests; medium = shared code, an
   interface, or business logic; high = money, data, auth, migrations, or several consumers. Foreman adds an
   impact investigation before the edits when the risk is medium or high, or when known consumers are left out.
Project names in steps/checks/projects must be registry names.
Images the user attached appear in the request as [Image #n: /path/file.png]. Look at them (read-only) when they
matter, and repeat the path in the instructions of every step whose worker needs to see the image.
When answering, talk about the work itself; don't mention internal task numbers (like #12) unless the user did.
You are read-only and nobody can approve tool permissions for you. Never ask the user to approve commands; if
something must be run to answer, add an investigate step or say what you could not verify.
""" + SUGGESTION_RULE


MODE_RULES = """\
Lifecycle modes. Choose one in "mode" and say why in "mode_reason" (a few words):
- "none" for a quick question answered now that is not lifecycle work (intent "answer").
- Otherwise the mode that fits the request and the project's current lifecycle state. Planning never assumes a
  blank slate: it starts from the current implementation. Changing an existing feature in a way that is large,
  ambiguous or risky -> planning first (the user can override); small, clear changes -> development.
- "feature": a short name for the feature or topic this belongs to; reuse a lifecycle name for the same feature.
- Read-only modes (research, planning, deployment) use investigate steps only. In qa, implement steps may only
  change tests. Steps get the chosen mode's worker guidance from Foreman."""
MODE_RULES_FIXED = """\
The user chose the "{mode}" mode: set "mode" to "{mode}" and plan within its rules below.
"feature": a short name for the feature or topic this belongs to; reuse a lifecycle name for the same feature."""


class BrainError(Exception):
    def __init__(self, message: str, kinds: Optional[List[str]] = None):
        super().__init__(message)
        self.kinds = kinds or []  # failure kind per provider tried (see providers.base), e.g. ["limit", "auth"]


class Brain:
    def __init__(self, providers: List[Provider], timeout: int = 600, model_for: Optional[Dict[str, str]] = None,
                 on_call: Optional[Callable[[str], bool]] = None, on_start: Optional[Callable[[str, str], None]] = None,
                 on_activity: Optional[Callable[[str, str, str], None]] = None,
                 on_fail: Optional[Callable[[str, RunResult], None]] = None):
        self.providers = providers  # preference order; first that succeeds wins
        self.timeout = timeout
        self.model_for = model_for or {}
        self.on_call = on_call or (lambda name: True)  # budget hook; False = provider over its limit
        self.on_start = on_start or (lambda name, purpose: None)
        self.on_activity = on_activity
        self.on_fail = on_fail or (lambda name, result: None)

    def call(self, prompt: str, schema: Dict[str, Any], cwd: str, extra_dirs: Optional[List[str]] = None,
             exclude: Optional[List[str]] = None, purpose: str = "plan", web: bool = False) -> Dict[str, Any]:
        errors, kinds = [], []
        for p in self.providers:
            if exclude and p.name in exclude:
                continue
            if not self.on_call(p.name):
                errors.append(f"{p.name}: daily limit reached")
                kinds.append("limit")
                continue
            self.on_start(p.name, purpose)
            act = (lambda text, _n=p.name: self.on_activity(_n, purpose, text)) if self.on_activity else None
            res = p.run(RunRequest(prompt=prompt, cwd=cwd, mode="read", extra_dirs=extra_dirs or [], persist=False,
                                   schema=schema, timeout=self.timeout, model=self.model_for.get(p.name),
                                   on_activity=act, web=web))
            if res.ok and isinstance(res.data, dict):
                res.data["_provider"] = p.name
                res.data["_errors"] = errors
                return res.data
            errors.append(f"{p.name}: {res.error or 'no structured output'}")
            kinds.append(res.kind)
            self.on_fail(p.name, res)
        raise BrainError("; ".join(errors) or "no provider available for planning", kinds)

    # ------------------------------------------------------------------ prompts
    @staticmethod
    def plan_prompt(request: str, cwd: str, registry_ctx: str, history: str, suggestions_seen: str,
                    task_ctx: str = "", conversation: str = "", modes_ctx: str = "", fixed_mode: str = "",
                    knowledge_ctx: str = "") -> str:
        parts = [
            "You are the planning layer of Foreman, an orchestrator that coordinates AI coding sessions across "
            "related projects. You decide scope and steps; worker sessions do the work; Foreman runs validation "
            "commands itself and reviews the result.",
            f"RECENT CONVERSATION (use it to resolve references like 'it', 'that', 'the same'):\n{conversation}"
            if conversation else "",
            f"REQUEST:\n{request}",
            f"CURRENT DIRECTORY: {cwd}",
            f"PROJECT REGISTRY:\n{registry_ctx}",
        ]
        if history:
            parts.append(f"RECENT TASKS IN THESE PROJECTS:\n{history}")
        if task_ctx:
            parts.append(f"TASK BEING CONTINUED (plan only what remains; re-verify conclusions where code changed):\n{task_ctx}")
        if knowledge_ctx:
            parts.append(f"PROJECT KNOWLEDGE (from each project's .foreman folder):\n{knowledge_ctx}")
        if suggestions_seen:
            parts.append(f"SUGGESTIONS ALREADY MADE OR DISMISSED (never repeat):\n{suggestions_seen}")
        if modes_ctx:
            parts.append(MODE_RULES_FIXED.format(mode=fixed_mode) if fixed_mode else MODE_RULES)
            parts.append(f"MODES:\n{modes_ctx}")
        parts.append(PLANNING_RULES)
        return "\n\n".join(p for p in parts if p)

    @staticmethod
    def document_prompt(task: Dict[str, Any], mode_title: str, output_spec: str, findings: str, checks: str) -> str:
        return "\n\n".join(x for x in [
            f"You are writing the {mode_title} document for Foreman, from work that AI sessions already did. "
            "Use only the findings below (you may read code to confirm a detail). Be concrete: file references, "
            "sources, commands. No filler. Markdown.",
            f"REQUEST:\n{task['request']}",
            f"THE DOCUMENT MUST CONTAIN:\n{output_spec}" if output_spec else "",
            "CONFIRMED DECISIONS:\n" + "\n".join(f"- {d['text']}" for d in task["decisions"][-12:])
            if task["decisions"] else "",
            "ANSWERED QUESTIONS:\n" + "\n".join(f"- {q['text']} -> {q['answer']}" for q in task["questions"]
                                                  if q.get("answer") and q.get("kind") == "business")
            if any(q.get("answer") and q.get("kind") == "business" for q in task["questions"]) else "",
            f"FINDINGS FROM THE STEPS:\n{findings}",
            f"CHECKS RUN BY FOREMAN:\n{checks}" if checks else "",
            "Return the full document in 'document', a title, and a two-sentence summary for the user. In "
            "'open_problems', list each defect, failed criterion or blocker that must be fixed or decided before "
            "moving on to the next stage (one line each; empty if none).",
        ] if x)

    @staticmethod
    def replan_prompt(task: Dict[str, Any], discoveries: List[str], registry_ctx: str) -> str:
        done = "\n".join(f"- [{s['status']}] {s['project']}: {s.get('summary') or s['instructions'][:120]}"
                         for s in task["steps"])
        return "\n\n".join([
            "You are the planning layer of Foreman. Workers discovered new information during a task. Decide whether "
            "it requires ADDITIONAL steps, checks or questions. Return only new steps/checks (empty lists if the "
            "discoveries do not affect the requested outcome). Keep intent and title unchanged.",
            f"REQUEST:\n{task['request']}",
            f"STEPS SO FAR:\n{done}",
            "DISCOVERIES:\n" + "\n".join(f"- {d}" for d in discoveries),
            f"PROJECT REGISTRY:\n{registry_ctx}",
            PLANNING_RULES,
        ])

    @staticmethod
    def review_prompt(task: Dict[str, Any], diffs: Dict[str, str], checks: List[Dict[str, Any]],
                      knowledge: str = "") -> str:
        diff_txt = "\n\n".join(f"### {p}\n{d or '(no changes)'}" for p, d in diffs.items())
        check_txt = "\n".join(f"- {c['project']}: `{c['command']}` -> {'PASS' if c['ok'] else 'FAIL'}"
                              + ("" if c["ok"] else f"\n  {c.get('tail', '')[-600:]}") for c in checks) or "(none run)"
        steps = "\n".join(f"- {s['project']}: {s.get('summary', '')}" for s in task["steps"] if s["kind"] == "implement")
        return "\n\n".join(x for x in [
            "You are an independent reviewer for Foreman. You did not write this change. Judge whether the request "
            "is fully and correctly achieved across all projects. Focus on correctness, consistency of interfaces "
            "between projects, missing pieces, and adequate tests. Ignore style nits. Use read-only tools to inspect "
            "code if the diff is not enough. Verdict 'revise' only for real problems, each with a concrete fix.",
            f"REQUEST:\n{task['request']}",
            "DECISIONS:\n" + ("\n".join(f"- {d['text']}" for d in task["decisions"]) or "(none)"),
            f"PROJECT KNOWLEDGE (confirmed business decisions must hold):\n{knowledge}" if knowledge else "",
            f"WORKER SUMMARIES:\n{steps or '(none)'}",
            f"VALIDATION RESULTS (run by Foreman):\n{check_txt}",
            f"DIFFS (uncommitted changes made by this task):\n{diff_txt}",
            "Small problems worth fixing now are issues (verdict revise), not suggestions. " + SUGGESTION_RULE,
        ] if x)


def task_context(task: Dict[str, Any], changes_since: Dict[str, List[str]]) -> str:
    """Compact task record for continuing work."""
    lines = [f"#{task['id']} {task['title']} [status: {task['status']}]", f"Original request: {task['request']}"]
    if task.get("mode"):
        lines.append(f"Mode: {task['mode']} ({task.get('mode_source')}) · feature: {task.get('feature', '')}")
    if task.get("document"):
        lines.append(f"Document written: {task['document']['project']}/{task['document']['path']}")
    for s in task["steps"]:
        lines.append(f"- step {s['id']} [{s['status']}] {s['kind']} in {s['project']}: "
                     f"{(s.get('summary') or s['instructions'])[:200]}")
    for d in task["decisions"][-8:]:
        lines.append(f"- decision ({d['by']}): {d['text']}")
    for q in task["questions"]:
        lines.append(f"- question: {q['text']} -> answer: {q.get('answer') or '(unanswered)'}")
    for r in task.get("previous_remaining") or task["remaining"]:
        lines.append(f"- remaining from last run: {r}")
    for e in [e for e in task["evidence"] if e["kind"] == "check"][-6:]:
        lines.append(f"- last check {e['project']}: {e['summary']} ({'pass' if e['ok'] else 'FAIL'})")
    for proj, files in changes_since.items():
        lines.append(f"- CODE CHANGED in {proj} since this task last ran: {', '.join(files[:15])}")
    return "\n".join(lines)


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)
