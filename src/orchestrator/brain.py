"""AI reasoning used by the orchestrator itself: planning (mode, scope, steps), re-planning, and turning a mode's
findings into its document (research brief, plan, QA report, release document); plus the schemas of worker and QA
verifier results (the engine runs those as steps).

Everything here returns validated JSON. The brain runs read-only; it may look at code to settle scope,
but is told to stop as soon as scope is clear (progressive context expansion).
"""

import json
from typing import Any, Callable, Dict, List, Optional

from orchestrator.providers.base import OUTPUT, Provider, RunRequest, RunResult
from orchestrator.util import check_result

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
    "header": {"type": "string", "description": "the topic in 1-2 words (at most 12 characters), e.g. Titles"},
    "options": {"type": "array", "items": {"type": "object", "properties": {
        "label": {"type": "string", "description": "the choice in a few words"},
        "description": {"type": "string", "description": "one plain sentence: what this choice means for the user"}},
        "required": ["label"]}},
    "default": {"type": "string", "description": "the label of the recommended option"}}, "required": ["text", "kind"]}}

# Worker results follow a versioned contract. Version 1 adds structured issues, the tests a worker ran, acceptance
# criteria results, risks and assumptions to the original fields; every field but status and summary is optional, so
# older results stay valid. A result with a newer "v" than Foreman knows is rejected as malformed.
PROTOCOL_VERSION = 1
ISSUES = {"type": "array", "items": {"type": "object", "properties": {
    "severity": {"type": "string", "enum": ["critical", "high", "medium", "low", "info"]},
    "title": _STR, "evidence": {"type": "string", "description": "file:line, command output or observation"},
    "files": _STRS, "repro": {"type": "string", "description": "how to reproduce it"}, "project": _STR,
    "criterion": {"type": "string", "description": "the acceptance criterion id it breaks, if any"},
    "verified": {"type": "boolean", "description": "true only if you reproduced it or saw it fail; false if suspected"}},
    "required": ["severity", "title"]}}
TESTS_RUN = {"type": "array", "items": {"type": "object", "properties": {
    "command": _STR, "outcome": {"type": "string", "enum": ["passed", "failed", "not_run"]}, "note": _STR},
    "required": ["command", "outcome"]}}
ACCEPTANCE_RESULTS = {"type": "array", "items": {"type": "object", "properties": {
    "id": _STR, "status": {"type": "string", "enum": ["pass", "fail", "untested"]}, "evidence": _STR},
    "required": ["id", "status"]}}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["answer", "change"]},
        "mode": _STR,
        "mode_reason": _STR,
        "feature": _STR,
        "title": _STR,
        "answer": _STR,
        "next_for_you": _STRS,
        "projects": {"type": "array", "items": {"type": "object", "properties": {
            "name": _STR, "role": {"type": "string", "enum": ["modify", "inspect", "test"]}, "why": _STR},
            "required": ["name", "role", "why"]}},
        "steps": {"type": "array", "items": {"type": "object", "properties": {
            "id": _STR, "project": _STR, "kind": {"type": "string", "enum": ["investigate", "implement"]},
            "instructions": _STR, "depends_on": _STRS, "read_projects": _STRS,
            "expected_minutes": {"type": "integer"},
            "weight": {"type": "string", "enum": ["light", "standard"]}},
            "required": ["id", "project", "kind", "instructions"]}},
        "checks": {"type": "array", "items": {"type": "object", "properties": {
            "project": _STR, "command": _STR, "why": _STR}, "required": ["project", "command"]}},
        "acceptance": {"type": "array", "items": _STR,
                       "description": "for a change: 2-6 short, testable criteria of the observable result"},
        "review_target": {"type": "object", "description": "set only to review changes that already exist",
                          "properties": {"kind": {"type": "string", "enum": ["uncommitted", "branch"]},
                                         "base": {"type": "string", "description": "branch: compared with (main)"}},
                          "required": ["kind"]},
        "questions": QUESTIONS,
        "decisions": _STRS,
        "suggestions": SUGGESTIONS,
        "impact": {"type": "object", "properties": {
            "summary": _STR, "risk": {"type": "string", "enum": ["low", "medium", "high"]},
            "consumers": _STRS, "tests": _STRS, "business_rules": _STRS, "interface_change": {"type": "boolean"},
            "needs_approval": {"type": "boolean", "description": "true only when the change has a significant security, "
                               "money, data-integrity, destructive or production impact; the user approves the plan first"}},
            "required": ["summary", "risk"]},
    },
    "required": ["intent", "title", "projects", "steps", "questions"],
}

BTW_SCHEMA = {"type": "object", "properties": {"answer": _STR}, "required": ["answer"]}

DOC_SCHEMA = {
    "type": "object",
    "properties": {"title": _STR, "document": _STR, "summary": _STR, "open_problems": _STRS, "next_for_you": _STRS},
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
        "next_for_you": _STRS,
        "v": {"type": "integer", "description": "result contract version (1)"},
        "issues": ISSUES,
        "tests": TESTS_RUN,
        "acceptance": ACCEPTANCE_RESULTS,
        "risks": _STRS,
        "assumptions": _STRS,
        "next_action": {"type": "string", "description": "what you recommend should happen next (advice only)"},
    },
    "required": ["status", "summary"],
}

# The QA verifier's result: the step contract plus a verdict and the tests it wants Foreman to run (it can't run them).
VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        **STEP_SCHEMA["properties"],
        "verdict": {"type": "string", "enum": ["approve", "revise"],
                    "description": "revise only for verified problems that must be fixed now"},
        "tests_requested": {"type": "array", "items": {"type": "object", "properties": {
            "project": _STR, "command": _STR, "why": _STR}, "required": ["project", "command"]},
            "description": "plain test commands (a test file or filter) for Foreman to run and report back"},
    },
    "required": ["status", "summary", "verdict"],
}

QUESTION_RULE = """\
Questions are read by the user, who may not know the code. Write each one in plain, everyday English: one short
sentence on what must be decided and why it matters to them. No abbreviations, internal names, file or field names,
codes or counts they would have to look up (say the source's full name; "more products from the same source", not
"candidates"). Give a "header" (the topic in 1-2 words) and 2-4 options, the recommended one first: each with a
"label" of a few words and a "description" of one plain sentence on what it means for them; put the recommended
label in "default". Before asking, read the conversation and the answered
questions: if the user already said what they want, even loosely or in other words, follow it and do not ask again.
If they said they did not understand, ask again far more simply."""

# How answers and documents are written for the user (config ui.output_style; chosen in /config).
OUTPUT_STYLES = {
    "default": ("Default", "Clear answers in short Markdown; what you have now"),
    "concise": ("Concise", "As short as possible: the answer and only the facts needed"),
    "explanatory": ("Explanatory", "Adds a short ★ Insight on why things work the way they do"),
    "learning": ("Learning", "Explains the reasoning and leaves small steps for you to try"),
    "proactive": ("Proactive", "Also points out the next action or risk you did not ask about"),
}
STYLE_RULES = {
    "concise": "OUTPUT STYLE: Concise. Answer in as few words as possible: the direct answer and only the facts needed, "
               "no headings unless there are several parts, no restating the question.",
    "explanatory": "OUTPUT STYLE: Explanatory. After the answer, add a short block headed '★ Insight' with 2-3 bullets "
                   "on why the code or data works the way it does here (patterns, trade-offs), in plain words.",
    "learning": "OUTPUT STYLE: Learning. Explain the reasoning briefly as you answer, and where the user could do a "
                "small part themselves (a command to run, a file to look at, a value to check), give it as a "
                "'Try it yourself' step instead of doing everything for them.",
    "proactive": "OUTPUT STYLE: Proactive. After the answer, point out in one or two lines the obvious next action or a "
                 "risk the user did not ask about, if there is one.",
}


def style_rule(style: str) -> str:
    return STYLE_RULES.get(style or "default", "")


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
   and prefer splitting "start the job in the background" from "process its result". Rate each step's "weight" so
   Foreman can spare effort: light = reading, summarising, a lookup or a one-spot edit; standard = anything
   else (a feature or fix, anything risky: money, auth, data, migrations, a shared interface, a hard bug).
6. Checks: commands Foreman runs itself to validate (tests, type-check, build) in modified or test projects.
   Prefer the registry's test command; narrow it when the suite is large. Non-destructive only: no deploys, DB
   resets, network installs or git commands.
7. Questions: only when a business/product rule cannot be inferred from code, docs or conventions and a wrong
   guess would be costly (kind "business"). Make technical decisions yourself and list them in "decisions".
8. Suggestions: at most 2. See the suggestion rule below; small in-scope fixes belong in the steps.
9. Reviewing existing work: when the user asks to review, QA or check changes that already exist (their
   uncommitted work, "my changes", a branch), set "review_target" (kind "uncommitted", or "branch" with "base",
   default main), list the projects to review, and plan no implement steps: Foreman's QA verifier reviews the diff
   and Foreman runs the checks. Nothing is fixed; problems are reported.
   Acceptance: for a change, put 2-6 short, testable criteria in "acceptance": the observable result the user asked
   for (behaviour, output, error handling), not implementation steps. None for answers.
10. Impact (whenever steps change code): what existing behaviour, consumers, tests and confirmed business
   decisions the change touches, and the risk: low = local and covered by tests; medium = shared code, an
   interface, or business logic; high = money, data, auth, migrations, or several consumers. Foreman adds an
   impact investigation before the edits when the risk is medium or high, or when known consumers are left out.
   Set "needs_approval" only for significant security, money, data-integrity, destructive or production impact:
   the user then approves the plan before anything changes. Ordinary complex changes don't need it.
Project names in steps/checks/projects must be registry names.
Images and files the user attached appear in the request as [Image #n: /path/file.png] or [File #n: /path], and
`@path` names a file or folder relative to the current directory. Look at them (read-only) when they matter, and
repeat the path in the instructions of every step whose worker needs it.
When answering, talk about the work itself; don't mention internal task numbers (like #12) unless the user did.
Write "answer" in Markdown so it is easy to scan: a one-line direct answer first, then short paragraphs or bullet
lists (never one long block); **bold** the key terms and numbers; put identifiers, paths and commands in `code`;
a short heading only when there are several parts. Plain facts, no filler.
When you answer directly (no steps), add "next_for_you": 1-3 short, concrete things the user could do or ask next, in
the user's words, given where the project stands and what they were after (e.g. "Ask me to run QA on the dataset",
"Review the 3 ABO gap records"). Leave it empty when nothing useful follows.
You are read-only and nobody can approve tool permissions for you. Never ask the user to approve commands; if
something must be run to answer, add an investigate step or say what you could not verify.
""" + QUESTION_RULE + "\n" + SUGGESTION_RULE


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
    def __init__(self, providers: List[Provider], timeout: int = 600,
                 choice_for: Optional[Callable[[str, str], Any]] = None,
                 on_call: Optional[Callable[[str], bool]] = None, on_start: Optional[Callable[[str, str], None]] = None,
                 on_activity: Optional[Callable[[str, str, str], None]] = None,
                 on_fail: Optional[Callable[[str, RunResult], None]] = None, stop_on_limit: bool = False,
                 on_result: Optional[Callable[[str, str, RunResult], None]] = None, prompt_dir: Optional[str] = None):
        self.providers = providers  # preference order; first that succeeds wins
        self.timeout = timeout
        self.choice_for = choice_for or (lambda provider, purpose: None)  # -> Choice(model, effort) or None
        self.on_call = on_call or (lambda name: True)  # budget hook; False = provider over its limit
        self.on_start = on_start or (lambda name, purpose: None)
        self.on_activity = on_activity
        self.on_fail = on_fail or (lambda name, result: None)
        self.stop_on_limit = stop_on_limit  # automatic switching is off: a usage limit ends the call (the caller waits)
        self.on_result = on_result or (lambda name, purpose, result: None)  # every run, for metrics
        self.prompt_dir = prompt_dir  # where prompts too long for a command line are written (see fit_prompt)

    def call(self, prompt: str, schema: Dict[str, Any], cwd: str, extra_dirs: Optional[List[str]] = None,
             exclude: Optional[List[str]] = None, purpose: str = "plan", web: bool = False) -> Dict[str, Any]:
        errors, kinds = [], []
        for p in self.providers:
            if exclude and p.name in exclude:
                continue
            if not self.on_call(p.name):
                errors.append(f"{p.name}: daily limit reached")
                kinds.append("limit")
                if self.stop_on_limit:
                    break
                continue
            self.on_start(p.name, purpose)
            act = (lambda text, _n=p.name: self.on_activity(_n, purpose, text)) if self.on_activity else None
            pick = self.choice_for(p.name, purpose)
            res = p.run(RunRequest(prompt=prompt, cwd=cwd, mode="read", extra_dirs=extra_dirs or [], persist=False,
                                   schema=schema, timeout=self.timeout, model=pick.model if pick else None,
                                   effort=pick.effort if pick else None, on_activity=act, web=web,
                                   prompt_dir=self.prompt_dir))
            self.on_result(p.name, purpose, res)
            if res.ok:  # a result that doesn't match its schema is a failure, never a partial success
                data, problems = check_result(res.data, schema)
                if data is not None:
                    data["_provider"] = p.name
                    data["_errors"] = errors
                    return data
                res = RunResult(False, res.text, res.session_id, error="its result didn't match the required format: "
                                + "; ".join(problems[:3]), duration=res.duration, raw=res.raw, kind=OUTPUT)
            errors.append(f"{p.name}: {res.error or 'no structured output'}")
            kinds.append(res.kind)
            self.on_fail(p.name, res)
            if self.stop_on_limit and res.kind == "limit":
                break
        raise BrainError("; ".join(errors) or "no provider available for planning", kinds)

    # ------------------------------------------------------------------ prompts
    @staticmethod
    def plan_prompt(request: str, cwd: str, registry_ctx: str, history: str, suggestions_seen: str,
                    task_ctx: str = "", conversation: str = "", modes_ctx: str = "", fixed_mode: str = "",
                    knowledge_ctx: str = "", style: str = "") -> str:
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
        parts.append(style_rule(style))
        return "\n\n".join(p for p in parts if p)

    @staticmethod
    def btw_prompt(question: str, cwd: str, registry_ctx: str, history: str, running: str = "",
                   conversation: str = "") -> str:
        """/btw: a quick side question, answered on the spot (read-only, no plan, nothing changed)."""
        return "\n\n".join(x for x in [
            "You are Foreman's assistant answering a quick side question while the user works (a 'by the way'). "
            "Answer it directly and briefly from the context below, reading a file or two only if you must. You are "
            "read-only: never change anything, plan work or start tasks. If it really needs real work, say so in one "
            "line and suggest how to ask for it.",
            f"RECENT CONVERSATION:\n{conversation}" if conversation else "",
            f"TASK RUNNING RIGHT NOW (don't interfere with it):\n{running}" if running else "",
            f"QUESTION:\n{question}",
            f"CURRENT DIRECTORY: {cwd}",
            f"PROJECT REGISTRY:\n{registry_ctx}",
            f"RECENT TASKS:\n{history}" if history else "",
            "Write the answer in short Markdown (a direct answer first, a few bullets at most) in 'answer'.",
        ] if x)

    @staticmethod
    def document_prompt(task: Dict[str, Any], mode_title: str, output_spec: str, findings: str, checks: str,
                        style: str = "") -> str:
        return "\n\n".join(x for x in [
            f"You are writing the {mode_title} document for Foreman, from work that AI sessions already did. "
            "Use only the findings below (you may read code to confirm a detail). Be concrete: file references, "
            "sources, commands. No filler. Markdown. Also give \"next_for_you\": 1-3 short, concrete things the user "
            "could do or ask next given this result.",
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
            style_rule(style),
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
