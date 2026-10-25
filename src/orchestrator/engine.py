"""Orchestration core.

    request ─► local fast path (status / resume / lifecycle "what's next", no AI)
            └► plan (brain: mode, scope, steps, checks, questions, impact)
                 ├─ risky change or consumers left out ─► impact investigation before any edit
                 ├─ blocking business question ─► needs_input (stop, ask user)
                 ├─ answer ─► done
                 └─ steps (DAG, parallel across projects, one session per step)
                      ├─ workers report new dependencies ─► learn relation, re-plan (bounded)
                      └─ validate: Foreman runs checks + diffs itself, independent review
                           └─ problems ─► fix rounds in the same sessions (bounded) ─► report
                 └─ mode with a document (R&D, planning, QA, deployment) ─► document saved in the project
            lifecycle: the project's .foreman records the stage reached and the recommended next mode

Deterministic: state, scheduling, provider/session selection, budget, check-command safety, git guards,
change detection, mode permissions (read-only / tests-only / web), impact gate, lifecycle and next step, report.
AI: mode choice and planning, the work itself, review, documents.
"""

import os
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any, Callable, Dict, List, Optional, Tuple

from orchestrator.brain import (DOC_SCHEMA, PLAN_SCHEMA, REVIEW_SCHEMA, STEP_SCHEMA, Brain, BrainError,
                                task_context)
from orchestrator.budget import Budget
from orchestrator.clipboard import image_dirs
from orchestrator.config import Config
from orchestrator.knowledge import ProjectKnowledge, slug
from orchestrator.modes import Mode, catalog, load_modes, resolve as resolve_mode
from orchestrator.providers.base import OWN_PROMPT_PREFIXES, Provider, RunRequest, RunResult
from orchestrator.registry import Registry
from orchestrator.report import notify, render_plan, render_report, render_status
from orchestrator.store import FINAL, Store
from orchestrator.util import (STOPPING, changed_since, diff_text, git_head, now_iso, run_process, short,
                               snapshot)

STATUS_RE = re.compile(
    r"^\s*(status\b|state\b|progress\b|"
    r"(what('?s| is| was)|where|how('?s| is| are)|show( me)?|give me)\b.{0,80}\b(status|state|progress|left|remaining|"
    r"stand|done so far|working on|blocked|blocker))", re.I)
# The instant status path is only for questions about recorded work, not about a project in general.
TASK_REF_RE = re.compile(r"(#\d+|\b(task|tasks|working on|work(ed)? on|left|remaining|progress|blocked|previous|last)\b)", re.I)
PROJECT_Q_RE = re.compile(r"\b(project|repo|repository|codebase|code base|service|app|application|branch)\b", re.I)
YES_RE = re.compile(r"^\s*(y|yes|yeah|yep|ok|okay|sure|go|go ahead|proceed|continue|do it|approved?|lgtm)\b", re.I)
NO_RE = re.compile(r"^\s*(n|no|nope|cancel|stop|abort|don'?t)\s*[.!]*\s*$", re.I)
AUTH_RE = re.compile(r"api key|expired|unauthori[sz]ed|\b401\b|not logged in|log ?in required|authenticat|credential",
                     re.I)
SMALL_TALK_RE = re.compile(
    r"^\s*(hi|hey|hello|hiya|yo|sup|good (morning|afternoon|evening|night)|thanks?( you)?( so much)?|thx|ty|"
    r"ok(ay)?|cool|nice|great|awesome|perfect|bye|goodbye|see you)( there| foreman| again)?\s*[!.?]*\s*$", re.I)
PREVIOUS_RE = re.compile(r"\b(previous|last|earlier|that|ongoing|current|i was|we were|my task|the task)\b", re.I)
RESUME_RE = re.compile(r"^\s*(please\s+)?(continue|resume|finish|pick up|carry on|keep going|complete the remaining)\b", re.I)
# Lifecycle: "what's next?" is answered from the project's records; "next" starts the recommended step.
NEXT_Q_RE = re.compile(r"^\s*(what('?s| is) next|what (should|do) (i|we) do next|where (are|do) we stand|"
                       r"(show )?(the )?lifecycle|which stage)\b.{0,60}$", re.I)
NEXT_DO_RE = re.compile(r"^\s*(ok(ay)?,?\s*)?(next|go to the next step|start the next step|do the next step|"
                        r"next step,? please|proceed to the next (step|stage))\s*[.!]*\s*$", re.I)
TEST_PATH_RE = re.compile(r"(^|/)(tests?|specs?|__tests__|e2e|cypress|playwright)/|(\.|_)(test|spec)\.[a-z]+$|"
                          r"(^|/)(tests?|specs?)\.[a-z]+$|"
                          r"(^|/)test_[^/]+\.py$|Test\.php$|_test\.go$", re.I)
NEXT_REASONS = {"planning": "turn the findings into a plan", "development": "implement it",
                "qa": "test it against the requirements, including related projects",
                "deployment": "prepare the release (checklist, rollback, verification)"}

SAFE_CHECK_PREFIXES = ("npm ", "npx ", "pnpm ", "yarn ", "bun ", "go ", "make ", "php ", "vendor/bin/",
                       "./vendor/bin/", "composer test", "composer run", "python", "pytest", "cargo ", "mvn ",
                       "gradle", "./gradlew", "node ", "deno ", "tox", "ruff", "mypy", "tsc", "eslint", "jest",
                       "vitest", "phpunit", "phpstan", "dotnet test", "bash -n", "sh -n", "true")
DENY_CHECK = re.compile(r"(rm\s+-rf|\bgit\s|\bsudo\b|curl[^|]*\|\s*(ba)?sh|migrate:fresh|migrate:reset|db:wipe|"
                        r"drop\s+(database|table)|docker\s+(rm|system|volume)|\bdeploy\b|\bpublish\b|kubectl|"
                        r"terraform\s+apply|`|\$\(|>\s*/dev/|\bnpm\s+(i|install|ci)\b|composer\s+(install|update|require)|"
                        r"\bmigrate(?!:status\b)|\bdb:seed\b|alembic\s+(upgrade|downgrade)|db\s+push|\brelease\b)",
                        re.I)


def clean_text(text: str) -> str:
    """Undo double-escaping some models apply inside JSON strings (literal \\n, wrapping quotes)."""
    text = (text or "").strip()
    if "\\n" in text and text.count("\n") < text.count("\\n"):
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "  ").replace('\\"', '"')
    if text.count('"') % 2 == 1 and (text.endswith('"') or text.startswith('"')):
        text = text.strip('"').strip()
    return text


class Interrupted(Exception):
    """Raised (from a signal handler) when the user stops a running task."""


def is_task_status_query(request: str) -> bool:
    """Status of recorded work (instant), as opposed to a question about a project's code (planner)."""
    if re.match(r"^\s*(show|give me)\b", request, re.I) and not TASK_REF_RE.search(request):
        return False  # "show VIP status in the API response" is a request, not a status query
    return bool(STATUS_RE.search(request)) and (bool(TASK_REF_RE.search(request)) or not PROJECT_Q_RE.search(request))


def check_command_allowed(cmd: str) -> Tuple[bool, str]:
    if DENY_CHECK.search(cmd):
        return False, "matches a destructive/unsafe pattern"
    for part in re.split(r"&&|;|\|\|", cmd):
        part = part.strip()
        if not part:
            continue
        if part.startswith("cd "):
            target = part[3:].strip()
            if target.startswith("/") or ".." in target.split("/"):
                return False, "cd outside the project"
            continue
        if not part.startswith(SAFE_CHECK_PREFIXES):
            return False, f"'{part.split()[0]}' is not a recognised test/build command"
    return True, ""


class Engine:
    def __init__(self, cfg: Config, providers: Dict[str, Provider], registry: Registry, store: Store,
                 out: Callable[[str], None] = print, visual: Optional[bool] = None,
                 notifier: Callable[[str, str], None] = notify):
        self.cfg = cfg
        self.providers = providers
        self.registry = registry
        self.store = store
        self.out = out
        self.visual = (cfg.display == "terminal") if visual is None else visual
        self.notifier = notifier if cfg.notify else (lambda *_: None)
        self.budget = Budget(cfg.home, {n: s.max_runs_per_day for n, s in cfg.providers.items()},
                             cfg.limits.warn_at_percent)
        self._lock = threading.RLock()
        self._last_activity: Dict[Any, Tuple[float, str]] = {}

    # ================================================================== entry points
    def handle(self, request: str, cwd: str, conversation: str = "", confirm_plan: bool = False,
               mode: Optional[str] = None, dry_run: bool = False) -> Dict[str, Any]:
        """Single entry for free-form requests from any interface. Returns {"task": ..., "report": str}.
        dry_run: plan only (see plan_only)."""
        task, msg = self.route(request, cwd, conversation, confirm_plan, mode)
        if not task:
            return {"task": None, "report": msg}
        return self.plan_only(task) if dry_run else self.execute(task)

    def route(self, request: str, cwd: str, conversation: str = "", confirm_plan: bool = False,
              mode: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], str]:
        """mode: a lifecycle mode the user chose (name or title), or None to let the planner recommend one."""
        """Decide what a request is without executing it: an instant answer (returned as text) or a task to run."""
        cwd = os.path.abspath(cwd)
        self.registry.ensure_scanned(cwd)
        if SMALL_TALK_RE.match(request):
            return None, self.small_talk(request, cwd)
        if NEXT_DO_RE.match(request):
            return self.prepare_next(cwd)
        if NEXT_Q_RE.match(request):
            return None, self.lifecycle_text(cwd)
        if is_task_status_query(request):
            res = self.status(self._ref_from(request), cwd, request)
            if res["task"] is not None or TASK_REF_RE.search(request):
                return None, res["report"]  # otherwise nothing recorded matches: let the planner answer it
        if RESUME_RE.search(request):
            return self.prepare_resume(self._ref_from(request), note=request, cwd=cwd)
        return self.prepare_new(request, cwd, conversation, confirm_plan, mode), ""

    def small_talk(self, text: str, cwd: str) -> str:
        """Instant, AI-free reply to greetings/thanks so a 'hey' never costs a model call."""
        t = text.strip().lower()
        if re.match(r"(thanks?|thx|ty)", t):
            return "You're welcome."
        if re.match(r"(bye|goodbye|see you|good night)", t):
            return "Bye! Your tasks and history are saved; just come back and say \"continue\"."
        if re.match(r"(ok|okay|cool|nice|great|awesome|perfect)", t):
            return "👍 What next?"
        proj = self.registry.project_for_path(cwd)
        open_tasks = [x for x in self.store.all() if x["status"] in ("needs_input", "blocked", "partial", "interrupted")
                      and (not proj or proj.name in x["projects"])]
        where = f"in {proj.name}" if proj else "here"
        msg = f"Hi! I'm ready {where}. Ask about the code, or describe a change and I'll plan and run it."
        if open_tasks:
            msg += f" You also have unfinished work: {short(open_tasks[0]['title'], 60)} (say \"continue\")."
        return msg

    def status(self, ref: Optional[str], cwd: str, request: str = "") -> Dict[str, Any]:
        """Current state of a task from the store, plus a deterministic freshness check. Never calls AI."""
        self.registry.ensure_scanned(cwd)
        anchor = self.registry.project_for_path(cwd)
        cands = self.store.resolve(ref, cwd=cwd, project_names=[anchor.name] if anchor else None,
                                   fallback=ref is None or bool(PREVIOUS_RE.search(request or "")))
        if not cands:
            return {"task": None, "report": "No tasks recorded yet" + (f" matching '{ref}'." if ref else ".")}
        if ref and len(cands) > 1:
            return {"task": None, "report": self._ambiguous(cands)}
        task = cands[0]
        lock = self.store.path(task["id"], "lock")
        if task["status"] in ("running", "planning", "queued") and not _pid_alive(_read_int(lock)):
            task["status"] = "interrupted"
            self.store.save(task)
        others = [t for t in cands[1:] if t["status"] not in FINAL]
        return {"task": task, "report": render_status(task, self._changes_since_last_run(task), others)}

    def resume(self, ref: Optional[str], note: str = "", cwd: str = ".") -> Dict[str, Any]:
        task, msg = self.prepare_resume(ref, note, cwd)
        return self.execute(task) if task else {"task": None, "report": msg}

    def answer(self, ref: str, text: str) -> Dict[str, Any]:
        task, msg = self.prepare_answer(ref, text)
        return self.execute(task) if task else {"task": None, "report": msg}

    def prepare_resume(self, ref: Optional[str], note: str = "", cwd: str = ".") -> Tuple[Optional[Dict[str, Any]], str]:
        """Pick the task to continue and mark it for re-planning. Does not execute."""
        cwd = os.path.abspath(cwd)
        self.registry.ensure_scanned(cwd)
        anchor = self.registry.project_for_path(cwd)
        cands = self.store.resolve(ref, cwd=cwd, project_names=[anchor.name] if anchor else None,
                                   fallback=ref is None or bool(PREVIOUS_RE.search(note or "")))
        unfinished = [t for t in cands if t["status"] not in FINAL]
        if not cands:
            return None, "No task found to continue" + (f" matching '{ref}'." if ref else ".")
        if (ref and len(cands) > 1) or (not ref and len(unfinished) > 1):
            return None, self._ambiguous(unfinished or cands)
        task = (unfinished or cands)[0]
        if note:
            task["decisions"].append({"text": f"User asked to continue: {note}", "by": "user", "at": now_iso()})
        task["replan"] = bool(note) or task["status"] != "planned"  # a dry-run plan is run as it stands
        task["dry_run"] = False
        task["status"] = "queued"
        self.store.save(task)
        return task, ""

    def prepare_answer(self, ref: str, text: str) -> Tuple[Optional[Dict[str, Any]], str]:
        cands = self.store.resolve(ref, fallback=False) if ref else self.store.resolve(None, active_only=True)
        cands = [t for t in cands if any(not q.get("answer") for q in t["questions"])] or cands
        if len(cands) != 1:
            return None, (self._ambiguous(cands) if cands else f"No task '{ref}'.")
        task = cands[0]
        open_q = [q for q in task["questions"] if not q.get("answer")]
        if not open_q:
            return None, f"#{task['id']} has no open questions."
        if NO_RE.match(text) and all(q.get("kind") in ("approval", "budget") for q in open_q):
            for q in open_q:
                q["answer"] = text
            task["status"] = "cancelled"
            self.store.save(task)
            self.store.event(task["id"], "cancelled", text=text)
            return None, f"Cancelled #{task['id']}. Nothing was changed."
        yes = bool(YES_RE.match(text))
        home = self._home_project(task)
        business = [q for q in open_q if q.get("kind") == "business"]
        targets = {q.get("project") for q in business} - {None} or ({home.name} if home and business else set())
        for name in targets:  # confirmed business rules outlive the task: keep them in the project(s)
            proj = self.registry.projects.get(name)
            if proj:
                asked = " / ".join(q["text"] for q in business if q.get("project") in (None, name))
                ProjectKnowledge(proj.path).add_decision(asked, text, task["id"], task.get("feature", ""))
        for q in open_q:
            q["answer"] = text
            if q.get("kind") == "budget" and yes:
                task["budget_ack"] = now_iso()
            if q.get("kind") == "approval" and yes:
                task["plan_approved"] = now_iso()
        task["decisions"].append({"text": f"User answered: {text}", "by": "user", "at": now_iso()})
        # Plain approvals continue the existing plan; anything else is feedback that needs a new plan.
        task["replan"] = not (yes and all(q.get("kind") in ("budget", "approval") for q in open_q))
        task["dry_run"] = False
        task["status"] = "queued"
        self.store.save(task)
        self.store.event(task["id"], "answered", text=text)
        return task, ""

    def prepare_new(self, request: str, cwd: str, conversation: str = "", confirm_plan: bool = False,
                    mode: Optional[str] = None, feature: Optional[str] = None,
                    parent: Optional[str] = None) -> Dict[str, Any]:
        task = self.store.new_task(request, os.path.abspath(cwd))
        task["status"] = "queued"
        task["conversation"] = conversation[-2500:]
        task["confirm_plan"] = confirm_plan
        chosen = resolve_mode(mode, self.modes(cwd))
        if chosen:
            task["mode"], task["mode_source"] = chosen, "user"
        if feature:
            task["feature"] = feature
        if parent:
            task["parent"] = parent
        self.store.save(task)
        return task

    # ================================================================== lifecycle
    def modes(self, cwd: str) -> Dict[str, Mode]:
        """Mode templates for a directory: built-in < ~/.foreman/modes < <project>/.foreman/modes."""
        proj = self.registry.project_for_path(cwd)
        return load_modes(self.cfg.home, proj.path if proj else None)

    def _mode(self, task: Dict[str, Any]) -> Optional[Mode]:
        return self.modes(task["cwd"]).get(task.get("mode") or "")

    def _home_project(self, task: Dict[str, Any]):
        """Project that holds the task's documents and lifecycle: the current directory's, else the first changed."""
        proj = self.registry.project_for_path(task["cwd"])
        if proj:
            return proj
        names = [n for n, e in task["projects"].items() if e.get("role") == "modify"] + list(task["projects"])
        return self.registry.projects.get(names[0]) if names else None

    def lifecycle_text(self, cwd: str) -> str:
        """Where the project's features stand and the recommended next step (instant, no AI)."""
        self.registry.ensure_scanned(cwd)
        proj = self.registry.project_for_path(cwd)
        modes = self.modes(cwd)
        names = " · ".join(m.title for m in modes.values())
        if not proj:
            return f"This folder is not a known project. Modes: {names}."
        kn = ProjectKnowledge(proj.path)
        feats = kn.lifecycle()
        if not feats:
            return (f"No lifecycle recorded for {proj.name} yet. Modes: {names}. Describe what you want and I'll "
                    "recommend a mode, or choose one yourself (/mode in the chat).")
        lines = [f"Lifecycle of {proj.name}:"]
        recent = sorted(feats.items(), key=lambda kv: str(kv[1].get("updated", "")), reverse=True)
        for key, f in recent[:8]:
            stage = modes[f["stage"]].title if f.get("stage") in modes else f.get("stage")
            nxt = modes[f["next"]].title if f.get("next") in modes else None
            lines.append(f"  {key:<24} {stage} ({str(f.get('status', '')).replace('_', ' ')}, {str(f.get('updated', ''))[:10]})"
                         + (f" → next: {nxt}, {f.get('next_reason', '')}" if nxt else ""))
        key, f = recent[0]
        if f.get("next") in modes:
            lines.append(f"Recommended: {modes[f['next']].title} for {key}. Say \"next\" to start it, or pick another mode.")
        dec = kn.decisions(1000)
        if dec:
            lines.append(f"{len(dec)} confirmed decision{'s' if len(dec) != 1 else ''} on record (.foreman/decisions.md).")
        return "\n".join(lines)

    def lifecycle_data(self, cwd: str) -> Dict[str, Any]:
        """The same facts as lifecycle_text, as data (instant, no AI)."""
        self.registry.ensure_scanned(cwd)
        proj = self.registry.project_for_path(cwd)
        modes = self.modes(cwd)
        if not proj:
            return {"project": None, "modes": list(modes), "features": {}, "decisions": 0}
        kn = ProjectKnowledge(proj.path)
        feats = kn.lifecycle()
        recent = sorted(feats.items(), key=lambda kv: str(kv[1].get("updated", "")), reverse=True)
        return {"project": proj.name, "modes": list(modes), "features": dict(recent),
                "recommended": ({"feature": recent[0][0], "mode": recent[0][1]["next"]}
                                if recent and recent[0][1].get("next") in modes else None),
                "decisions": len(kn.decisions(1000))}

    def prepare_next(self, cwd: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """Start the recommended next mode for the most recent piece of work here."""
        self.registry.ensure_scanned(cwd)
        proj = self.registry.project_for_path(cwd)
        cands = [t for t in self.store.all() if t.get("next") and not t.get("next_started")
                 and t["status"] not in ("running", "queued", "planning")
                 and (not proj or proj.name in t["projects"] or self.registry.project_for_path(t["cwd"]) == proj)]
        if not cands:
            return None, "Nothing is waiting for a next step here. " + self.lifecycle_text(cwd)
        prev = cands[0]
        nxt = prev["next"]
        mode = self.modes(cwd).get(nxt["mode"])
        title = mode.title if mode else nxt["mode"]
        doc = prev.get("document") or {}
        request = (f"{title} for \"{prev.get('feature') or prev['title']}\": {nxt.get('reason', '')}.\n"
                   f"Previous step ({prev.get('mode')}, task #{prev['id']}): {short(prev.get('summary') or prev['title'], 400)}"
                   + (f"\nIts document: {doc['project']}/{doc['path']}" if doc else "")
                   + f"\nOriginal request: {prev['request']}")
        task = self.prepare_new(request, prev["cwd"], mode=nxt["mode"], feature=prev.get("feature"), parent=prev["id"])
        prev["next_started"] = task["id"]
        self.store.save(prev)
        return task, ""

    def _next_step(self, task: Dict[str, Any], mode: Mode) -> Optional[Dict[str, str]]:
        """Recommended next mode, from the mode's template and the outcome (deterministic)."""
        if task["status"] in ("interrupted", "cancelled", "failed", "needs_input"):
            return None
        latest = {}
        for e in task["evidence"]:
            if e["kind"] in ("check", "review"):
                latest[(e["kind"], e["project"], e.get("command"))] = e
        problems = task["status"] in ("partial", "blocked") or bool(task["remaining"]) or \
            any(not e["ok"] for e in latest.values())
        if problems and mode.writes == "code":
            return {"mode": mode.name, "reason": "finish the remaining items"}
        if problems and mode.name == "qa":
            return {"mode": "development", "reason": "fix what QA found"}
        if problems:
            return {"mode": mode.name, "reason": "resolve what is blocking it"}
        if not mode.next:
            return None
        return {"mode": mode.next, "reason": NEXT_REASONS.get(mode.next, "")}

    # ================================================================== execution
    def plan_only(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """Dry run: plan the task (read-only planner, impact check included) and stop before any worker runs, any
        check runs or anything is written to a project. `resume` on the task then runs exactly this plan."""
        task["status"], task["dry_run"] = "planning", True
        self.store.save(task)
        try:
            planned = self._plan(task)
        except Interrupted:
            task["status"], planned = "interrupted", False
        if planned:
            task["status"] = "needs_input" if self._open_questions(task) else "planned"
        self.store.event(task["id"], "dry_run", status=task["status"])
        self.store.save(task)
        return {"task": task, "report": render_plan(task)}

    def execute(self, task: Dict[str, Any]) -> Dict[str, Any]:
        lock = self.store.path(task["id"], "lock")
        if os.path.exists(lock) and _pid_alive(_read_int(lock)) and _read_int(lock) != os.getpid():
            return {"task": task, "report": f"#{task['id']} is already running (pid {_read_int(lock)})."}
        with open(lock, "w") as f:
            f.write(str(os.getpid()))
        task["runs"] += 1
        task["status"] = "running"
        task["notices"] = []
        # Remaining items are recomputed every run; the previous list is kept as context for re-planning.
        task["previous_remaining"], task["remaining"] = task["remaining"] or task.get("previous_remaining", []), []
        for s in task["steps"]:
            if s["status"] == "running":  # interrupted run
                s["status"] = "pending"
        self.store.save(task)
        try:
            self._run(task)
        except Interrupted:
            task["status"] = "interrupted"
            self.store.event(task["id"], "interrupted")
        except Exception as e:  # never leave a task stuck in "running"
            task["status"] = "failed"
            task["remaining"].append(f"Orchestrator error: {e}")
            self.store.event(task["id"], "error", error=repr(e))
        finally:
            self._finalize(task)
            try:
                os.remove(lock)
            except OSError:
                pass
        report = render_report(task, self.store.suggestions())
        with open(self.store.path(task["id"], "report.md"), "w", encoding="utf-8") as f:
            f.write(report)
        if task["status"] != "interrupted":  # the user stopped it; no notification needed
            self.notifier(f"Foreman #{task['id']}: {task['status']}", short(task.get("summary") or task["title"], 180))
        return {"task": task, "report": report}

    def _run(self, task: Dict[str, Any]) -> None:
        if not task["steps"] or task.pop("replan", False) or not task["intent"]:
            if not self._plan(task):
                return
        if self._open_questions(task):
            task["status"] = "needs_input"
            return
        if task["intent"] == "answer" and task.get("answer") and not self._pending(task):
            mode = self._mode(task)
            if mode and mode.document:  # the planner wrote the result directly: it is the document
                self._document(task, direct=True)
            task["status"] = "answered"
            return
        if not self._plan_approved(task) or not self._budget_ok(task):
            task["status"] = "needs_input"
            return
        self._baseline(task)
        while True:
            self._run_steps(task)
            if self._open_questions(task):
                task["status"] = "needs_input"
                return
            discoveries = task.pop("discoveries", [])
            if discoveries and task["replans"] < self.cfg.limits.max_replans:
                task["replans"] += 1
                if self._replan(task, discoveries) and self._pending(task):
                    continue
            break
        if any(s["status"] in ("failed", "blocked", "skipped") for s in task["steps"]):
            task["status"] = "blocked"
        mode = self._mode(task)
        if any(s["kind"] == "implement" and s["status"] == "done" for s in task["steps"]):
            self._verify(task)
            if task["status"] == "needs_input":
                return
        elif mode and mode.writes != "code" and mode.checks:
            # QA / deployment: Foreman runs the projects' checks itself; failures are findings for the user
            failing = [c for c in self._run_checks(task, {}) if not c["ok"]]
            task["remaining"] += [f"{c['project']}: `{c['command']}` fails" for c in failing]
        if mode and mode.document:
            self._document(task)
            if task["status"] == "running":
                task["status"] = "done"
            return
        if task["intent"] == "answer":
            task["answer"] = "\n\n".join(s.get("summary", "") + ("\n" + "\n".join("- " + f for f in s.get("findings", []))
                                                                  if s.get("findings") else "")
                                         for s in task["steps"] if s["status"] == "done") or task.get("answer")
            if task["status"] != "blocked":
                task["status"] = "answered"
            return

    # ------------------------------------------------------------------ planning
    def _plan(self, task: Dict[str, Any]) -> bool:
        resume = task["runs"] > 1 or bool(task["steps"])
        changes = self._changes_since_last_run(task) if resume else {}
        sel = self.registry.select(task["request"], task["cwd"], extra=list(task["projects"]))
        self.registry.refresh([p.name for p in sel.focus])
        ctx = self.registry.render_context(sel)
        names = {p.name for p in sel.focus} | {p.name for _, p in sel.neighbors}
        history = "\n".join(
            f"- #{t['id']} [{t['status']}] {t['title']}" + (f" | remaining: {'; '.join(t['remaining'][:2])}" if t["remaining"] else "")
            for t in self.store.all() if t["id"] != task["id"] and names & set(t["projects"]))[:2500]
        seen = "\n".join(f"- [{s['status']}] {s['text']}" for s in self.store.suggestions()
                         if not s["project"] or s["project"] in names)[:1500]
        modes = self.modes(task["cwd"])
        fixed = task.get("mode") if task.get("mode_source") == "user" else ""
        knowledge = "\n".join(k for k in (ProjectKnowledge(p.path).context(p.name) for p in sel.focus) if k)
        prompt = Brain.plan_prompt(task["request"], task["cwd"], ctx, history, seen,
                                   task_context(task, changes) if resume else "", task.get("conversation", ""),
                                   modes_ctx=catalog(modes, only=fixed or None), fixed_mode=fixed or "",
                                   knowledge_ctx=knowledge)
        cwd = sel.anchor.path if sel.anchor else (sel.focus[0].path if sel.focus else task["cwd"])
        extra = [p.path for p in sel.focus if p.path != cwd][:6] + image_dirs(task["request"])
        self._say(task, "planning…")
        before = snapshot(cwd)
        try:
            plan = self._brain(task).call(prompt, PLAN_SCHEMA, cwd, extra, purpose="plan",
                                          web=bool(fixed and modes[fixed].web))
        except BrainError as e:
            task["status"] = "blocked"
            task["remaining"].append(f"Planning failed: {e}")
            self.store.event(task["id"], "plan_failed", error=str(e))
            return False
        self.store.event(task["id"], "plan", plan=plan)
        self._provider_errors(task, plan)
        touched = changed_since(cwd, before)
        if touched:
            self._notice(task, f"The planner ({plan.get('_provider')}) changed files while it should only read: "
                               f"{', '.join(touched[:6])}")
        self._apply_plan(task, plan, replace_pending=True)
        mode = self._mode(task)
        if mode:
            task["mode_title"] = mode.title
            self.store.event(task["id"], "mode", mode=mode.name, title=mode.title, source=task.get("mode_source"),
                             reason=task.get("mode_reason", ""))
        self._ensure_impact(task)
        if changes:
            task["evidence"].append({"kind": "freshness", "project": ", ".join(changes), "ok": True, "at": now_iso(),
                                     "summary": "code changed since last run; plan re-verified against current state"})
        return True

    def _replan(self, task: Dict[str, Any], discoveries: List[str]) -> bool:
        sel = self.registry.select(task["request"], task["cwd"], extra=list(task["projects"]))
        try:
            plan = self._brain(task).call(Brain.replan_prompt(task, discoveries, self.registry.render_context(sel)),
                                          PLAN_SCHEMA, sel.anchor.path if sel.anchor else task["cwd"], purpose="replan")
        except BrainError as e:
            task["notices"].append(f"Could not re-plan after discoveries: {e}")
            return False
        self.store.event(task["id"], "replan", discoveries=discoveries, plan=plan)
        before = len(task["steps"])
        self._apply_plan(task, plan, replace_pending=False)
        return len(task["steps"]) > before

    def _apply_plan(self, task: Dict[str, Any], plan: Dict[str, Any], replace_pending: bool) -> None:
        if replace_pending:
            task["intent"] = plan.get("intent") or task["intent"] or "change"
            task["title"] = short(plan.get("title") or task["title"], 80)
            task["answer"] = clean_text(plan.get("answer") or "") or None
            task["answered_by"] = plan.get("_provider")
            task["steps"] = [s for s in task["steps"] if s["status"] == "done"]
            task["checks"] = []
            if not task.get("mode"):  # decided once per task; moving on to another mode is the user's "next"
                task["mode"] = resolve_mode(plan.get("mode"), self.modes(task["cwd"]))
                task["mode_source"] = "auto" if task["mode"] else None
                task["mode_reason"] = short(plan.get("mode_reason") or "", 160)
            task["feature"] = task.get("feature") or slug(plan.get("feature") or task["title"])
            if plan.get("impact"):
                task["impact"] = plan["impact"]
        for p in plan.get("projects", []):
            proj = self.registry.find(p.get("name", ""))
            if not proj:
                task["notices"].append(f"Planner referenced unknown project '{p.get('name')}' (ignored)")
                continue
            mode = self._mode(task)
            role = "inspect" if p["role"] == "modify" and mode and mode.writes == "none" else p["role"]
            entry = task["projects"].setdefault(proj.name, {"path": proj.path, "role": role, "why": p.get("why", "")})
            if role == "modify" or entry["role"] != "modify":
                entry["role"] = role
        prefix = f"r{task['runs']}.{task['replans']}."
        ids = {s.get("id"): prefix + re.sub(r"^(r\d+\.\d+\.)+", "", str(s.get("id"))) for s in plan.get("steps", [])}
        for s in plan.get("steps", []):
            proj = self.registry.find(s.get("project", ""))
            if not proj:
                task["notices"].append(f"Step for unknown project '{s.get('project')}' dropped")
                continue
            task["projects"].setdefault(proj.name, {"path": proj.path, "why": "has a planned step", "role": "inspect"})
            reads = [self.registry.find(r) for r in s.get("read_projects", [])]
            mode = self._mode(task)
            if s["kind"] == "implement" and mode and mode.writes == "none":  # read-only mode: analyse, don't edit
                s = {**s, "kind": "investigate", "instructions": f"Read-only ({mode.title} mode: no files are changed). "
                     f"Analyse and describe precisely what this would require, with file references: {s['instructions']}"}
                task["projects"][proj.name]["role"] = "inspect" if task["projects"][proj.name].get("role") == "modify" \
                    else task["projects"][proj.name].get("role")
            elif s["kind"] == "implement":
                task["projects"][proj.name]["role"] = "modify"
            task["steps"].append({
                "id": ids[s.get("id")], "project": proj.name, "kind": s["kind"], "instructions": s["instructions"],
                "depends_on": [ids[d] for d in s.get("depends_on", []) if d in ids],
                "read_projects": [r.name for r in reads if r and r.name != proj.name],
                "status": "pending", "attempts": 0, "provider": None, "session_id": None, "summary": "",
            })
        for c in plan.get("checks", []):
            proj = self.registry.find(c.get("project", ""))
            if proj and not any(x["project"] == proj.name and x["command"] == c["command"] for x in task["checks"]):
                ok, why = check_command_allowed(c["command"])
                task["checks"].append({"project": proj.name, "command": c["command"], "why": c.get("why", ""),
                                       "allowed": ok, "blocked_reason": why})
        for d in plan.get("decisions", []) or []:
            task["decisions"].append({"text": d, "by": f"planner ({plan.get('_provider')})", "at": now_iso()})
        for q in plan.get("questions", []) or []:
            if q.get("kind") == "business" and not any(x["text"] == q["text"] for x in task["questions"]):
                task["questions"].append({"text": q["text"], "kind": "business", "options": q.get("options", []),
                                          "default": q.get("default"), "answer": None})
            elif q.get("kind") == "technical":
                task["decisions"].append({"text": f"Open technical question left to workers: {q['text']}",
                                          "by": "planner", "at": now_iso()})
        task.setdefault("suggestion_buffer", []).extend(plan.get("suggestions", []) or [])

    def _ensure_impact(self, task: Dict[str, Any]) -> None:
        """Before code changes: if the planner rates the risk medium/high, or a known consumer of a project being
        changed is left out of the plan, run a read-only impact investigation first (implement steps wait for it)."""
        mode = self._mode(task)
        impl = [s for s in task["steps"] if s["status"] == "pending" and s["kind"] == "implement"]
        if not impl or (mode and mode.writes != "code"):
            return
        if any(s["kind"] == "investigate" and s["status"] in ("pending", "done") for s in task["steps"]):
            return  # the plan already investigates first
        modified = {s["project"] for s in impl}
        # Relations point from the consumer to what it uses (admin-ui --related--> orders-api): changing the
        # provider can break its consumers, not the other way round. Shared contracts cut both ways.
        left_out = set()
        for r in self.registry.relations:
            if r.type == "mentions":
                continue
            pairs = [(r.dst, r.src)] + ([(r.src, r.dst)] if r.type == "shares_contract_with" else [])
            for changed, other in pairs:
                if changed in modified and other not in task["projects"] and other in self.registry.projects:
                    left_out.add(other)
        risk = (task.get("impact") or {}).get("risk", "low")
        if risk == "low" and not left_out:
            return
        home = impl[0]["project"]
        for name in sorted(left_out):
            task["projects"][name] = {"path": self.registry.projects[name].path, "role": "inspect",
                                      "why": "known consumer or dependency of a project being changed (impact check)"}
        reads = sorted((left_out | modified | {n for n, e in task["projects"].items() if e.get("role") == "test"})
                       - {home})[:5]
        sid = f"r{task['runs']}.{task['replans']}.impact"
        task["steps"].append({
            "id": sid, "project": home, "kind": "investigate", "status": "pending", "attempts": 0, "provider": None,
            "session_id": None, "summary": "", "depends_on": [], "read_projects": reads, "impact": True,
            "instructions": (
                "Impact analysis before anything is changed. The planned change: " + task["request"] + "\n"
                "Find everything the change could affect, here and in the related projects: callers and consumers of the "
                "code or interfaces involved, tests that cover them, and confirmed business decisions or rules that "
                "constrain it. List concrete risks and regressions with file references, and what the implementation "
                "must keep compatible. If a business rule is unclear and a wrong guess would be costly, return it as a "
                "business question. Do not change files.")})
        for s in impl:
            s["depends_on"].append(sid)
        self.store.event(task["id"], "impact", risk=risk, consumers=sorted(left_out))

    # ------------------------------------------------------------------ steps
    def _run_steps(self, task: Dict[str, Any]) -> None:
        running: Dict[Any, Dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=max(1, self.cfg.limits.max_parallel)) as pool:
            while True:
                by_id = {s["id"]: s for s in task["steps"]}
                busy_projects = {s["project"] for s in running.values()}
                for s in task["steps"]:
                    if s["status"] != "pending":
                        continue
                    deps = [by_id.get(d) for d in s["depends_on"]]
                    if any(d and d["status"] in ("failed", "blocked", "skipped", "needs_input") for d in deps):
                        s["status"] = "skipped"
                        s["summary"] = "skipped: a step it depends on did not complete"
                        continue
                    if all(d is None or d["status"] == "done" for d in deps) and s["project"] not in busy_projects \
                            and len(running) < self.cfg.limits.max_parallel:
                        s["status"] = "running"
                        busy_projects.add(s["project"])
                        running[pool.submit(self._run_step, task, s)] = s
                self.store.save(task)
                if not running:
                    break
                done, _ = wait(list(running), return_when=FIRST_COMPLETED)
                for fut in done:
                    step = running.pop(fut)
                    try:
                        fut.result()
                    except Exception as e:
                        step["status"] = "failed"
                        step["summary"] = f"orchestrator error: {e}"

    def _run_step(self, task: Dict[str, Any], step: Dict[str, Any]) -> None:
        proj = self.registry.projects[step["project"]]
        before = snapshot(proj.path)
        head_before = git_head(proj.path)
        prompt = self._step_prompt(task, step, proj)
        tried: List[str] = []
        result: Optional[RunResult] = None
        while True:
            choice = self._choose(task, step, proj.path, tried)
            if not choice:
                break
            provider, session_id, fork, why = choice
            tried.append(provider.name)
            ok, warn = self.budget.acquire(provider.name)
            if not ok:
                self._notice(task, warn)
                continue
            if warn:
                self._notice(task, warn)
            step["attempts"] += 1
            self._say(task, f"{step['kind']} {step['project']} via {provider.name}" + (f" ({why})" if why else ""))
            self.store.event(task["id"], "step_start", step=step["id"], provider=provider.name, project=step["project"],
                             step_kind=step["kind"], session=session_id, fork=fork, why=why)
            mode = self._mode(task)
            result = provider.run(RunRequest(
                web=bool(mode and mode.web and step["kind"] == "investigate"),
                prompt=prompt, cwd=proj.path, mode="write" if step["kind"] == "implement" else "read",
                session_id=session_id, fork=fork, schema=STEP_SCHEMA, timeout=self.cfg.limits.step_timeout_sec,
                extra_dirs=[self.registry.projects[r].path for r in step["read_projects"] if r in self.registry.projects]
                + image_dirs(task["request"]),
                visual=self.visual, title=f"Foreman #{task['id']} · {step['kind']} · {step['project']}",
                on_activity=lambda text, _p=provider.name: self._activity(task["id"], _p, step["project"], text)))
            self.store.write_log(task["id"], f"step-{step['id']}-{provider.name}", result.raw or result.text or str(result.error))
            with self._lock:
                if result.session_id:
                    task["sessions"].append({"provider": provider.name, "session_id": result.session_id,
                                             "project": step["project"], "step": step["id"], "forked_from": session_id if fork else None,
                                             "at": now_iso()})
            if result.ok:
                step["provider"], step["session_id"] = provider.name, result.session_id or session_id
                break
            if STOPPING.is_set():
                break  # the user stopped the task: not a provider failure
            self._record_failure(provider.name, result.error or "")
            self.store.event(task["id"], "step_failed", step=step["id"], provider=provider.name, error=result.error)
            self._notice(task, f"{provider.name} failed on {step['project']}: {short(result.error or '', 160)}")
            # A failed resume may be a stale session: retry the same provider fresh before moving on.
            if session_id and provider.name not in step.setdefault("fresh_retry", []):
                step["fresh_retry"].append(provider.name)
                tried.remove(provider.name)
                step["session_id"] = None
                step["no_reuse"] = True
        with self._lock:
            self._record_step(task, step, proj, result, before, head_before)

    def _record_step(self, task, step, proj, result, before, head_before) -> None:
        if not result or not result.ok:
            step["status"] = "failed"
            step["summary"] = "no provider could complete this step" + (f": {short(result.error or '', 200)}" if result else "")
            self.store.event(task["id"], "step_done", step=step["id"], project=step["project"], status="failed",
                             summary=step["summary"])
            return
        data = result.data or {"status": "done", "summary": short(result.text, 600)}
        step["status"] = {"done": "done", "blocked": "blocked", "needs_input": "needs_input"}.get(data.get("status"), "done")
        step["summary"] = clean_text(data.get("summary", ""))
        step["findings"] = data.get("findings", [])
        step["files_changed"] = data.get("files_changed", [])
        self.store.event(task["id"], "step_done", step=step["id"], project=step["project"], status=step["status"],
                         summary=step["summary"])
        if git_head(proj.path) != head_before:
            self._notice(task, f"Policy violation: a worker created commits in {proj.name} (HEAD moved). Review them.")
        if step["kind"] == "investigate":
            touched = changed_since(proj.path, before)
            if touched:
                self._notice(task, f"Read-only step changed files in {proj.name}: {', '.join(touched[:8])}")
        mode = self._mode(task)
        if step["kind"] == "implement" and mode and mode.writes == "tests":
            app = [f for f in changed_since(proj.path, before) if not TEST_PATH_RE.search(f)]
            if app:
                self._notice(task, f"QA rule broken: application files changed in {proj.name} (QA may only change "
                                   f"tests): {', '.join(app[:8])}. Review them.")
        for q in data.get("questions", []) or []:
            if q.get("kind") == "business":
                task["questions"].append({"text": q["text"], "kind": "business", "options": q.get("options", []),
                                          "default": q.get("default"), "answer": None, "from_step": step["id"]})
                if step["status"] == "done":
                    step["status"] = "needs_input"
        for dep in data.get("dependencies", []) or []:
            other = self.registry.find(dep.get("project", "")) or (
                self.registry.add_project(dep["project"]) if os.path.isdir(os.path.expanduser(dep.get("project", ""))) else None)
            if not other or other.name == proj.name:
                continue
            self.registry.link(proj.name, other.name, dep.get("type") or "related", dep.get("evidence", ""),
                               source="learned", confidence="medium", task=task["id"])
            if other.name not in task["projects"]:
                task.setdefault("discoveries", []).append(
                    f"{proj.name} {dep.get('type', 'relates to')} {other.name}: {dep.get('evidence', '')}")
        task.setdefault("suggestion_buffer", []).extend(data.get("suggestions", []) or [])

    def _choose(self, task, step, cwd, tried) -> Optional[Tuple[Provider, Optional[str], bool, str]]:
        """Pick provider + session. Continuity first, then forking a recent user session, then a new session."""
        avail = [p for n, p in self.providers.items() if n not in tried and self._usable(n)]
        if not avail:
            return None
        mode = self._mode(task)
        if mode and mode.web and step["kind"] == "investigate":  # research steps go to providers that can browse
            avail = [p for p in avail if p.capabilities.web] + [p for p in avail if not p.capabilities.web]
        if step.get("session_id") and step.get("provider") and not step.get("no_reuse"):
            prov = self.providers.get(step["provider"])
            if prov and prov in avail:
                return prov, step["session_id"], False, "continuing its session"
        if not step.get("no_reuse"):
            for s in reversed(task["sessions"]):
                prov = self.providers.get(s["provider"])
                if s["project"] == step["project"] and prov in avail and prov.capabilities.resume and \
                        not s.get("reviewer"):
                    return prov, s["session_id"], False, "continuing this task's session for the project"
        if self.cfg.reuse_sessions == "fork" and not step.get("no_reuse"):
            ours = {s["session_id"] for t in self.store.all() for s in t.get("sessions", [])}
            horizon = time.time() - self.cfg.reuse_max_age_hours * 3600
            for prov in avail:
                if not (prov.capabilities.fork and prov.capabilities.session_discovery):
                    continue
                for sess in prov.discover_sessions(cwd):
                    if sess.updated_at >= horizon and sess.session_id not in ours and \
                            not sess.title.startswith(OWN_PROMPT_PREFIXES):
                        age = int((time.time() - sess.updated_at) / 3600)
                        return prov, sess.session_id, True, f"forked your session '{short(sess.title, 40)}' ({age}h old)"
                break  # only the preferred provider's sessions are worth forking
        return avail[0], None, False, ""

    def _step_prompt(self, task, step, proj) -> str:
        reads = "\n".join(f"- {r}: {self.registry.projects[r].path}" for r in step["read_projects"] if r in self.registry.projects)
        rels = "\n".join("- " + self.registry._rel_line(r, proj.name) for r in self.registry.relations_of(proj.name)
                         if r.type != "mentions")[:2000]
        decisions = "\n".join(f"- {d['text']}" for d in task["decisions"][-10:])
        answers = "\n".join(f"- Q: {q['text']}\n  A: {q['answer']}" for q in task["questions"] if q.get("answer"))
        others = "\n".join(f"- {s['project']} ({s['kind']}): {s['summary'][:300]}"
                           + "".join(f"\n    · {short(f, 200)}" for f in (s.get("findings") or [])[:8]
                                     if s["kind"] == "investigate")
                           for s in task["steps"] if s["status"] == "done" and s["id"] != step["id"] and s.get("summary"))
        mode = self._mode(task)
        knowledge = "\n".join(k for k in [ProjectKnowledge(proj.path).context(proj.name)] + [
            ProjectKnowledge(self.registry.projects[r].path).context(r) for r in step["read_projects"]
            if r in self.registry.projects] if k)[:3500]
        rules = (
            "- Edit files only inside this project. Other projects are read-only for you.\n"
            "- Complete the change fully, including routine refactors and tests it needs, and run the project's "
            "relevant tests. Fix straightforward problems you find along the way.\n"
            "- Do not invent business/product rules. If one is needed and cannot be inferred from code, docs or "
            "the decisions below, stop and return it as a question of kind 'business'.\n"
            if step["kind"] == "implement" else
            "- Read-only step: do not create, modify or delete files.\n"
            "- Answer precisely with file references; stop as soon as you have enough evidence.\n")
        if mode and mode.writes == "tests" and step["kind"] == "implement":
            rules += "- QA: change TEST files only. Never change application code; report bugs as findings instead.\n"
        if step["kind"] == "investigate":
            rules += ("- Web research is allowed for this step; cite URLs.\n" if mode and mode.web
                      else "- Work from the code and documents; do not search the web.\n")
        return "\n\n".join(x for x in [
            "You are a worker session coordinated by Foreman, an orchestrator for related projects.",
            f"OVERALL REQUEST:\n{task['request']}",
            f"YOUR STEP ({step['kind']}) in project {proj.name} at {proj.path}:\n{step['instructions']}",
            f"MODE: {mode.title}. {mode.summary}\n{mode.section('worker')}" if mode else "",
            f"PROJECT KNOWLEDGE (from .foreman; confirmed decisions must be followed):\n{knowledge}" if knowledge else "",
            f"OTHER PROJECTS YOU MAY READ:\n{reads}" if reads else "",
            f"KNOWN RELATIONS OF {proj.name}:\n{rels}" if rels else "",
            f"RESULTS OF OTHER STEPS:\n{others}" if others else "",
            f"DECISIONS ALREADY MADE:\n{decisions}" if decisions else "",
            f"USER ANSWERS:\n{answers}" if answers else "",
            "RULES:\n" + rules +
            "- Report any dependency on another project you discover (API consumer/provider, shared contract, tests) "
            "with evidence (project name or absolute path).\n"
            "- Fix small related problems yourself instead of suggesting them. Suggestions are only for major, "
            "out-of-scope things the user must decide; usually none.\n"
            "- Keep the summary short and factual.",
        ] if x)

    # ------------------------------------------------------------------ validation
    def _baseline(self, task) -> None:
        for name, entry in task["projects"].items():
            if entry["role"] == "modify" and "baseline" not in entry and os.path.isdir(entry["path"]):
                entry["baseline"] = snapshot(entry["path"])

    def _verify(self, task) -> None:
        mode = self._mode(task)
        # QA reports problems instead of fixing them (fixes would touch application code): no fix rounds.
        max_rounds = 0 if mode and mode.writes == "tests" else self.cfg.limits.max_fix_rounds
        for rnd in range(max_rounds + 1):
            changed = {n: changed_since(e["path"], e["baseline"]) for n, e in task["projects"].items()
                       if e.get("baseline")}
            for n, files in changed.items():
                task["projects"][n]["changed_files"] = files
            checks = self._run_checks(task, changed)
            failing = [c for c in checks if not c["ok"]]
            review = self._review(task, changed, checks) if any(changed.values()) else None
            issues = list(review.get("issues", [])) if review and review.get("verdict") == "revise" else []
            if not any(changed.values()):
                task["notices"].append("Implementation steps reported done but no file changes were detected.")
            if not failing and not issues:
                task["status"] = "done" if task["status"] != "blocked" else "blocked"
                for a in task["projects"]:
                    for b in task["projects"]:
                        if a < b:
                            self.registry.mark_verified(a, b)
                return
            if rnd == max_rounds:
                task["status"] = "partial"
                task["remaining"] += [f"{c['project']}: `{c['command']}` still failing" for c in failing]
                task["remaining"] += [f"{i['project']}: {i['problem']}" for i in issues]
                self._notice(task, "QA found problems; they are listed under Remaining for development to fix."
                             if max_rounds == 0 else f"Stopped after {rnd} fix round(s); escalating remaining problems to you.")
                return
            task["fix_rounds"] += 1
            self._schedule_fixes(task, failing, issues)
            self._run_steps(task)
            if self._open_questions(task):
                task["status"] = "needs_input"
                return

    def _project_checks(self, name: str) -> Dict[str, str]:
        """Check commands by kind: detected by the registry, overridden by the repo's .foreman/checks.yaml."""
        proj = self.registry.projects.get(name)
        if not proj:
            return {}
        out = {**({"test": proj.test} if proj.test else {}), **proj.checks}
        out.update(ProjectKnowledge(proj.path).checks())
        return out

    def _run_checks(self, task, changed: Dict[str, List[str]]) -> List[Dict[str, Any]]:
        checks = list(task["checks"])
        mode = self._mode(task)

        def add(name: str, command: str, why: str) -> None:
            if command and not any(c["project"] == name and c["command"] == command for c in checks):
                ok, reason = check_command_allowed(command)
                checks.append({"project": name, "command": command, "why": why, "allowed": ok, "blocked_reason": reason})

        for name, files in changed.items():  # tests of every changed project
            if files and not any(c["project"] == name for c in checks):
                add(name, self._project_checks(name).get("test", ""), "registry test command")
        for name, e in task["projects"].items():
            if e.get("role") == "test" and any(changed.values()):  # regression: test projects related to the change
                add(name, self._project_checks(name).get("test", ""), "regression tests of a related project")
            if mode and mode.writes != "code":  # QA / deployment: every check kind the mode asks for
                for kind in mode.checks:
                    add(name, self._project_checks(name).get(kind, ""), f"{mode.title} {kind} check")
        results = []
        for c in checks:
            proj = self.registry.projects.get(c["project"])
            if not proj:
                continue
            if not c.get("allowed", True):
                self._notice(task, f"Skipped check `{c['command']}` in {c['project']}: {c['blocked_reason']} "
                                   "(run it yourself or approve it in the registry test command)")
                continue
            self._say(task, f"check {c['project']}: {c['command']}")
            res = run_process(c["command"], cwd=proj.path, timeout=self.cfg.limits.check_timeout_sec, shell=True)
            tail = (res.out + "\n" + res.err).strip()[-1500:]
            ok = res.code == 0
            log = self.store.write_log(task["id"], f"check-{c['project']}", f"$ {c['command']}\n{res.out}\n{res.err}")
            summary = f"`{c['command']}` {'passed' if ok else 'FAILED'}" + (" (timed out)" if res.timed_out else "")
            task["evidence"].append({"kind": "check", "project": c["project"], "command": c["command"], "ok": ok,
                                     "summary": summary, "at": now_iso(), "log": log})
            self.store.event(task["id"], "check", project=c["project"], command=c["command"], ok=ok)
            results.append({**c, "ok": ok, "tail": tail})
        return results

    def _review(self, task, changed, checks) -> Optional[Dict[str, Any]]:
        diffs = {n: diff_text(task["projects"][n]["path"], files) for n, files in changed.items()}
        implementers = {s["provider"] for s in task["steps"] if s["kind"] == "implement" and s.get("provider")}
        names = list(changed)
        cwd = task["projects"][names[0]]["path"]
        extra = [task["projects"][n]["path"] for n in names[1:]]
        brain = self._brain(task, prefer_not=implementers)
        try:
            knowledge = "\n".join(k for k in (ProjectKnowledge(task["projects"][n]["path"]).context(n) for n in names) if k)
            review = brain.call(Brain.review_prompt(task, diffs, checks, knowledge[:4000]), REVIEW_SCHEMA, cwd, extra,
                                purpose="review")
        except BrainError as e:
            self._notice(task, f"Independent review unavailable: {e}")
            return None
        self._provider_errors(task, review)
        reviewer = review.get("_provider")
        independent = "different provider" if reviewer not in implementers else "fresh session, same provider"
        task["evidence"].append({"kind": "review", "project": ", ".join(names), "ok": review["verdict"] == "approve",
                                 "summary": f"{review['verdict']} by {reviewer} ({independent}): {short(review.get('summary', ''), 200)}",
                                 "at": now_iso()})
        task.setdefault("suggestion_buffer", []).extend(review.get("suggestions", []) or [])
        self.store.event(task["id"], "review", review=review)
        return review

    def _schedule_fixes(self, task, failing, issues) -> None:
        per_project: Dict[str, List[str]] = {}
        for c in failing:
            per_project.setdefault(c["project"], []).append(
                f"Validation `{c['command']}` fails. Output tail:\n{c.get('tail', '')[-1200:]}\n"
                "If this failure is pre-existing and unrelated to this task, do not change unrelated code; "
                "say so in the summary.")
        for i in issues:
            proj = self.registry.find(i.get("project", "")) or self.registry.find(list(task["projects"])[0])
            per_project.setdefault(proj.name, []).append(f"Reviewer: {i['problem']}" + (f" Fix: {i['fix']}" if i.get("fix") else ""))
        for name, problems in per_project.items():
            prev = next((s for s in reversed(task["steps"]) if s["project"] == name and s["kind"] == "implement"), None)
            task["steps"].append({
                "id": f"fix{task['fix_rounds']}.{name}", "project": name, "kind": "implement",
                "instructions": "Fix these problems found during validation of your change:\n- " + "\n- ".join(problems),
                "depends_on": [], "read_projects": prev["read_projects"] if prev else [], "status": "pending",
                "attempts": 0, "provider": prev.get("provider") if prev else None,
                "session_id": prev.get("session_id") if prev else None, "summary": "",
            })

    # ------------------------------------------------------------------ documents
    def _document(self, task: Dict[str, Any], direct: bool = False) -> None:
        """Write the mode's result (research brief, plan, QA report, release document) into the project's
        .foreman folder. direct: the planner already wrote it as its answer; otherwise one AI call turns the
        steps' findings into the document (falling back to the raw findings if no provider can)."""
        mode, home = self._mode(task), self._home_project(task)
        if not mode or not mode.document or not home:
            return
        findings = "\n\n".join(
            f"## {s['project']} ({s['kind']})\n{s.get('summary', '')}" + "".join(f"\n- {f}" for f in s.get("findings") or [])
            for s in task["steps"] if s["status"] == "done")
        checks = "\n".join(f"- {e['project']}: {e['summary']}" for e in task["evidence"] if e["kind"] == "check")
        if direct:
            text, summary = task.get("answer") or "", short(task.get("answer") or "", 300)
        else:
            try:
                data = self._brain(task).call(
                    Brain.document_prompt(task, mode.title, mode.section("output"), findings or "(none)", checks),
                    DOC_SCHEMA, home.path, [e["path"] for n, e in task["projects"].items() if n != home.name][:5],
                    purpose="document")
                self._provider_errors(task, data)
                text, summary = clean_text(data.get("document", "")), clean_text(data.get("summary", ""))
                task["remaining"] += [short(p, 200) for p in data.get("open_problems") or [] if p.strip()][:8]
            except BrainError as e:
                self._notice(task, f"Could not write the {mode.title} document with an AI provider ({short(str(e), 120)}); "
                                   "saved the raw findings instead.")
                text, summary = f"# {task['title']}\n\n{findings}\n\n## Checks\n{checks or '(none)'}", task["title"]
        if not text.strip():
            return
        path = ProjectKnowledge(home.path).save_document(mode.document, task.get("feature") or task["title"], text,
                                                         task["id"])
        task["document"] = {"project": home.name, "path": path, "kind": mode.document}
        task["intent"] = "answer"
        task["answer"] = summary or task.get("answer")
        self.store.event(task["id"], "document", project=home.name, path=path)

    # ------------------------------------------------------------------ finalize
    def _finalize(self, task) -> None:
        if task["status"] == "running":
            task["status"] = "done"
        if task["status"] in ("blocked", "partial", "failed"):
            for s in task["steps"]:
                if s["status"] in ("failed", "blocked", "skipped", "pending") and s["summary"] not in task["remaining"]:
                    task["remaining"].append(f"{s['project']} ({s['kind']}): {s['summary'] or s['instructions'][:160]}")
        task["remaining"] = list(dict.fromkeys(task["remaining"]))[-12:]
        for name, entry in task["projects"].items():
            if os.path.isdir(entry["path"]):
                entry["after"] = snapshot(entry["path"])
        added = self.store.add_suggestions(task["id"], task.pop("suggestion_buffer", []), limit=3)
        task["suggestions"] = [s["id"] for s in added]
        task["outcome"] = {"done": "achieved", "answered": "achieved", "partial": "partially achieved",
                           "needs_input": "waiting for your input", "blocked": "blocked"}.get(task["status"], task["status"])
        task["summary"] = self._summary(task)
        self._record_lifecycle(task)
        self.store.save(task)
        self.store.event(task["id"], "finished", status=task["status"])

    def _record_lifecycle(self, task) -> None:
        """Note the stage this feature reached, and the recommended next mode, in each involved project."""
        mode = self._mode(task)
        if not mode or task["status"] in ("interrupted", "cancelled"):
            return
        task["next"] = self._next_step(task, mode)
        if task["next"]:
            nxt = self.modes(task["cwd"]).get(task["next"]["mode"])
            task["next"]["title"] = nxt.title if nxt else task["next"]["mode"]
        home = self._home_project(task)
        names = {n for n, e in task["projects"].items() if e.get("role") == "modify"} | ({home.name} if home else set())
        doc = (task.get("document") or {}).get("path")
        for name in sorted(names):
            proj = self.registry.projects.get(name)
            if not proj or not os.path.isdir(proj.path):
                continue
            try:
                ProjectKnowledge(proj.path).update_feature(
                    task.get("feature") or task["title"], task["title"], mode.name, task["status"], task["id"],
                    task.get("summary", ""), (task.get("next") or {}).get("mode"), (task.get("next") or {}).get("reason", ""),
                    doc if home and name == home.name else None)
            except OSError as e:
                self._notice(task, f"Could not update {name}/.foreman: {e}")

    def _summary(self, task) -> str:
        if task.get("answer") and task["intent"] == "answer":
            return short(task["answer"], 300)
        done = [s for s in task["steps"] if s["status"] == "done" and s["kind"] == "implement"]
        return "; ".join(f"{s['project']}: {short(s['summary'], 120)}" for s in done) or task["title"]

    # ------------------------------------------------------------------ helpers
    def _brain(self, task: Optional[Dict[str, Any]] = None, prefer_not: Optional[set] = None) -> Brain:
        names = list(self.providers)
        if self.cfg.brain in names:
            names.remove(self.cfg.brain)
            names.insert(0, self.cfg.brain)
        if prefer_not:
            names = [n for n in names if n not in prefer_not] + [n for n in names if n in prefer_not]
        provs = [self.providers[n] for n in names if self._usable(n)]
        models = {n: s.brain_model for n, s in self.cfg.providers.items() if s.brain_model}
        tid = task["id"] if task else None
        return Brain(provs, timeout=self.cfg.limits.brain_timeout_sec, model_for=models,
                     on_call=lambda name: self.budget.acquire(name)[0],
                     on_start=lambda name, purpose: tid and self.store.event(tid, "thinking", provider=name, purpose=purpose),
                     on_activity=(lambda name, purpose, text: self._activity(tid, name, purpose, text)) if tid else None,
                     on_fail=lambda name, err: self._brain_failed(tid, name, err))

    # ------------------------------------------------------------------ provider health
    def _health(self) -> Dict[str, Any]:
        from orchestrator.util import read_json
        return read_json(os.path.join(self.cfg.home, "health.json"), {}) or {}

    def unhealthy(self) -> Dict[str, str]:
        """Providers skipped because they recently failed to authenticate: name -> reason."""
        now = time.time()
        return {n: h["error"] for n, h in self._health().items() if h.get("until", 0) > now}

    def _usable(self, name: str) -> bool:
        p = self.providers.get(name)
        return bool(p and p.available()[0] and name not in self.unhealthy())

    def _record_failure(self, name: str, error: str) -> None:
        if not AUTH_RE.search(error or ""):
            return
        from orchestrator.util import write_json
        with self._lock:
            h = self._health()
            h[name] = {"error": short(error, 160), "at": now_iso(), "until": time.time() + 6 * 3600}
            write_json(os.path.join(self.cfg.home, "health.json"), h)

    def _brain_failed(self, task_id: Optional[str], name: str, error: str) -> None:
        self._record_failure(name, error)
        if task_id:
            self.store.event(task_id, "thinking_failed", provider=name, error=short(error, 160))

    def _activity(self, task_id: Optional[str], provider: str, where: str, text: str) -> None:
        """Live worker/planner activity for front ends (throttles assistant-text chatter)."""
        if not task_id:
            return
        key = (task_id, provider, where)
        last = self._last_activity.get(key, (0.0, ""))
        if text == last[1] or (text.startswith("· ") and time.time() - last[0] < 2):
            return
        self._last_activity[key] = (time.time(), text)
        for p in sorted(self.registry.projects.values(), key=lambda p: -len(p.path)):  # show paths as project/…
            text = text.replace(p.path.rstrip("/") + "/", p.name + "/").replace(p.path, p.name)
        m = re.match(r"^(\w+)\((.*)\)$", text, re.S)
        if m and len(m.group(2)) > 90:
            text = f"{m.group(1)}({m.group(2)[:89]}…)"
        self.store.event(task_id, "activity", provider=provider, where=where, text=text)

    def _provider_errors(self, task, data: Dict[str, Any]) -> None:
        for err in data.pop("_errors", []) or []:
            self._notice(task, f"Skipped {short(err, 180)}")

    def _plan_approved(self, task) -> bool:
        """Cross-project changes wait for the user's go-ahead when the interface asked for it (chat)."""
        pending = [s for s in task["steps"] if s["status"] == "pending" and s["kind"] == "implement"]
        projects = sorted({s["project"] for s in pending})
        if not task.get("confirm_plan") or task.get("plan_approved") or len(projects) < 2:
            return True
        lines = [f"This changes {len(projects)} projects:"]
        lines += [f"  - {s['project']}: {short(s['instructions'], 110)}" for s in pending]
        checks = [f"{c['project']}: {c['command']}" for c in task["checks"]]
        if checks:
            lines.append(f"  then checks: {'; '.join(checks)}")
        lines.append("Go ahead? (yes / no / or tell me what to change)")
        text = "\n".join(lines)
        if not any(q["text"] == text and not q.get("answer") for q in task["questions"]):
            task["questions"].append({"text": text, "kind": "approval", "options": [], "answer": None})
        return False

    def _budget_ok(self, task) -> bool:
        if task.get("budget_ack"):
            return True
        needed = [n for n in self.providers if self._usable(n)][:1]
        over = [n for n in needed if self.budget.over_warning(n)]
        if not over:
            return True
        text = (f"{', '.join(over)} is at {self.budget.percent(over[0])}% of its daily run limit. "
                f"Continue with this task? (answer yes to proceed)")
        if not any(q["text"] == text and not q.get("answer") for q in task["questions"]):
            task["questions"].append({"text": text, "kind": "budget", "options": ["yes", "no"], "answer": None})
        return False

    def _changes_since_last_run(self, task) -> Dict[str, List[str]]:
        out = {}
        for name, entry in task["projects"].items():
            after = entry.get("after")
            if after and os.path.isdir(entry["path"]):
                files = changed_since(entry["path"], after)
                if files:
                    out[name] = files
        return out

    @staticmethod
    def _open_questions(task) -> List[Dict[str, Any]]:
        return [q for q in task["questions"] if not q.get("answer")]

    @staticmethod
    def _pending(task) -> bool:
        return any(s["status"] == "pending" for s in task["steps"])

    def _notice(self, task, msg: str) -> None:
        with self._lock:
            if msg and msg not in task["notices"]:
                task["notices"].append(msg)

    def _say(self, task, msg: str) -> None:
        self.out(f"  #{task['id']} {msg}")

    @staticmethod
    def _ref_from(request: str) -> Optional[str]:
        m = re.search(r"#(\d+)", request)
        if m:
            return m.group(1)
        stripped = RESUME_RE.sub("", request)
        stripped = re.sub(r"\b(the|previous|last|task|work|i was working on|state|status|progress|of|and|what|is|"
                          r"where|current|my|on|with|it)\b", " ", stripped, flags=re.I)
        stripped = " ".join(stripped.split()).strip(" ?.!,")
        return stripped if len(stripped) > 3 else None

    @staticmethod
    def _ambiguous(cands) -> str:
        lines = ["Several tasks match; which one? (use its number)"]
        lines += [f"  #{t['id']} [{t['status']}] {t['title']}" for t in cands[:6]]
        return "\n".join(lines)


def _read_int(path: str) -> int:
    try:
        with open(path) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
