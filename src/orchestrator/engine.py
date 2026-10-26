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

from orchestrator.brain import (BTW_SCHEMA, DOC_SCHEMA, PLAN_SCHEMA, QUESTION_RULE, REVIEW_SCHEMA, STEP_SCHEMA,
                                Brain, BrainError, task_context)
from orchestrator.budget import Budget
from orchestrator.clipboard import image_dirs
from orchestrator.config import Choice, Config
from orchestrator.knowledge import ProjectKnowledge, slug
from orchestrator.modes import Mode, catalog, load_modes, resolve as resolve_mode
from orchestrator.providers.base import (AUTH, CONTEXT, LIMIT, OUTPUT, OWN_PROMPT_PREFIXES, STOPPED, TIMEOUT,
                                         TRANSIENT, Provider, RunRequest, RunResult)
from orchestrator.registry import Registry
from orchestrator.report import clock, notify, render_plan, render_report, render_status
from orchestrator.store import FINAL, Store
from orchestrator.util import (STOPPING, changed_since, diff_text, git_head, now_iso, read_json, run_process, short,
                               snapshot, write_json)

STATUS_RE = re.compile(
    r"^\s*(status\b|state\b|progress\b|"
    r"(what('?s| is| was)|where|how('?s| is| are)|show( me)?|give me)\b.{0,80}\b(status|state|progress|left|remaining|"
    r"stand|done so far|working on|blocked|blocker))", re.I)
# The instant status path is only for questions about recorded work, not about a project in general.
TASK_REF_RE = re.compile(r"(#\d+|\b(task|tasks|working on|work(ed)? on|left|remaining|progress|blocked|previous|last)\b)", re.I)
PROJECT_Q_RE = re.compile(r"\b(project|repo|repository|codebase|code base|service|app|application|branch)\b", re.I)
YES_RE = re.compile(r"^\s*(y|yes|yeah|yep|ok|okay|sure|go|go ahead|proceed|continue|do it|approved?|lgtm)\b", re.I)
GIT_INIT_RE = re.compile(r"\bgit init\b", re.I)
BARE_YES_RE = re.compile(r"^\s*(y|yes|yeah|yep|ok|okay|sure|go|go ahead|proceed|do it|approved?|lgtm)\s*[.!]*\s*$", re.I)
NO_RE = re.compile(r"^\s*(n|no|nope|cancel|stop|abort|don'?t)\s*[.!]*\s*$", re.I)
# Why an attempt stopped, as the next worker is told.
STOP_REASONS = {TIMEOUT: "Foreman's time limit", LIMIT: "the provider's usage limit", TRANSIENT: "a network or API error",
                AUTH: "a login or billing problem", CONTEXT: "a full context window", OUTPUT: "a missing final report",
                "crash": "the process stopping unexpectedly", "interrupted": "Foreman being stopped or closed",
                STOPPED: "you stopping the task"}
TRANSIENT_WAITS = (30, 120, 300)
# "close #26", "close task 26: finished by hand", "cancel #5" (the user settles a task Foreman can't judge itself)
CLOSE_RE = re.compile(r"^\s*(?:please\s+)?(close|cancel|drop|abandon)\s+(?:the\s+)?(?:task\s+)?#(\d+)\b[\s:,.-]*(.*)$",
                      re.I | re.S)
SKIP_RE = re.compile(r"^\s*skip( (this|the) step)?\b", re.I)
ANSWER_ITEM_RE = re.compile(r"^\s*(?:q\s*)?(\d+)\s*[.):\-]\s*", re.I)
SMALL_TALK_RE = re.compile(
    r"^\s*(hi|hey|hello|hiya|yo|sup|good (morning|afternoon|evening|night)|thanks?( you)?( so much)?|thx|ty|"
    r"ok(ay)?|cool|nice|great|awesome|perfect|bye|goodbye|see you)( there| foreman| again)?\s*[!.?]*\s*$", re.I)
PREVIOUS_RE = re.compile(r"\b(previous|last|earlier|that|ongoing|current|i was|we were|my task|the task)\b", re.I)
RESUME_RE = re.compile(r"^\s*(please\s+)?(continue|resume|finish|pick up|carry on|keep going|complete the remaining)\b", re.I)
# Lifecycle: "what's next?" is answered from the project's records; "next" starts the recommended step.
NEXT_Q_RE = re.compile(r"^\s*(what('?s| is) next|what (should|do) (i|we) do next|where (are|do) we stand|"
                       r"(show )?(the )?lifecycle|which stage)\b.{0,60}$", re.I)
_MODE_WORDS = r"(next( step)?|plan(ning)?|r&d|research|dev(elopment)?|qa|deployment)( mode)?"
NEXT_DO_RE = re.compile(r"^\s*(ok(ay)?,?\s*)?(next|go to the next step|start the next step|do the next step|"
                        r"next step,? please|proceed to the next (step|stage)|go for (the )?" + _MODE_WORDS +
                        r"|start (the )?" + _MODE_WORDS + r"|do the plan|plan it|let'?s (go|plan|start))\s*[.!]*\s*$", re.I)
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


def question_options(q: Dict[str, Any]) -> Dict[str, Any]:
    """A question's choices as stored: option labels with the recommended one first, a description per option ("" if
    none) and a short header. Options may come as {label, description} objects or as plain strings."""
    labels, notes = [], []
    for o in q.get("options") or []:
        label = str(o.get("label") or "") if isinstance(o, dict) else str(o)
        if label.strip() and label.strip() not in labels:
            labels.append(label.strip())
            notes.append(" ".join(str(o.get("description") or "").split()) if isinstance(o, dict) else "")
    default = str(q.get("default") or "").strip() or None
    if default and default not in labels:  # the recommendation written out in full instead of its label
        hit = next((i for i, l in enumerate(labels) if default.startswith(l) or l.startswith(default)), None)
        default = labels[hit] if hit is not None else default
        if hit is None:
            labels, notes = [default] + labels, [""] + notes
    if default:
        i = labels.index(default)
        labels, notes = [labels[i]] + labels[:i] + labels[i + 1:], [notes[i]] + notes[:i] + notes[i + 1:]
    header = " ".join(str(q.get("header") or "").split())[:12]
    return {"options": labels, "descriptions": notes, "default": default, "header": header}


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
        self._fixed_visual, self._notifier = visual, notifier
        self.visual = (cfg.display == "terminal") if visual is None else visual
        self.notifier = notifier if cfg.notify else (lambda *_: None)
        self.budget = Budget(cfg.home, {n: s.max_runs_per_day for n, s in cfg.providers.items()},
                             cfg.limits.warn_at_percent)
        self._lock = threading.RLock()
        self._last_activity: Dict[Any, Tuple[float, str]] = {}

    def reload_config(self) -> None:
        """Re-read config.yaml into this running engine, after a menu or another process changed it."""
        from orchestrator.config import load_config
        self.cfg = load_config(self.cfg.home)
        if self._fixed_visual is None:
            self.visual = self.cfg.display == "terminal"
        self.notifier = self._notifier if self.cfg.notify else (lambda *_: None)
        order = [n for n in self.cfg.providers if n in self.providers]
        self.providers = {n: self.providers[n] for n in order} | {n: p for n, p in self.providers.items() if n not in order}

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
        m = CLOSE_RE.match(request)
        if m:
            return None, self.close(m.group(2), m.group(3).strip(), cancel=m.group(1).lower() != "close")["report"]
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
        """Pick the task to continue. Does not execute. The task carries on with its plan where it stopped, in the
        sessions it had (a cut-off step resumes its session); it is re-planned only when the user adds instructions,
        the code changed under it since it last ran, or nothing of the plan is left to run."""
        cwd = os.path.abspath(cwd)
        self.registry.ensure_scanned(cwd)
        anchor = self.registry.project_for_path(cwd)
        cands = self.store.resolve(ref, cwd=cwd, project_names=[anchor.name] if anchor else None,
                                   fallback=ref is None or bool(PREVIOUS_RE.search(note or "")))
        if not cands and ref and not re.fullmatch(r"#?\d+", ref.strip()):
            # "continue, but use the CSV dumps": the words are instructions, not a task name; continue the latest here
            ref = None
            cands = self.store.resolve(None, cwd=cwd, project_names=[anchor.name] if anchor else None)
        cands = [t for t in cands if not t.get("closed")]  # settled by the user: never picked up again
        unfinished = [t for t in cands if t["status"] not in FINAL]
        if not cands:
            return None, "No task found to continue" + (f" matching '{ref}'." if ref else ".")
        if (ref and len(cands) > 1) or (not ref and len(unfinished) > 1):
            return None, self._ambiguous(unfinished or cands)
        task = (unfinished or cands)[0]
        extra = _instructions(note)
        if extra:
            task["decisions"].append({"text": f"User asked to continue: {note}", "by": "user", "at": now_iso()})
        left = [s for s in task["steps"] if not _settled(s)]
        task["replan"] = task["status"] != "planned" and (  # a dry-run plan is run as it stands
            bool(extra) or not left or bool(self._changes_since_last_run(task)))
        if not task["replan"]:
            self._reopen_steps(task)
        task["dry_run"] = False
        task["status"] = "queued"
        self.store.save(task)
        return task, ""

    def close(self, ref: str, note: str = "", cancel: bool = False) -> Dict[str, Any]:
        """The user settles a task by hand: it was finished another way (close: done) or isn't wanted any more
        (cancel). Nothing in the projects is touched. Its unfinished steps and open questions are closed with it, so a
        later "continue" never picks it up again; a closed task recommends the next mode like a finished one."""
        cands = self.store.resolve(ref, fallback=False)
        if len(cands) != 1:
            return {"task": None, "report": self._ambiguous(cands) if cands else f"No task '{ref}'."}
        task = cands[0]
        lock = self.store.path(task["id"], "lock")
        if os.path.exists(lock) and _pid_alive(_read_int(lock)):
            return {"task": task, "report": f"#{task['id']} is running; stop it first (Esc in the chat)."}
        if task["status"] in FINAL:
            return {"task": task, "report": f"#{task['id']} is already {task['status']}."}
        how = "cancelled by you" if cancel else "closed by you"
        for s in task["steps"]:
            if not _settled(s):
                s.update(status="skipped", skipped_by_user=True, summary=f"{how} before it ran" if s["status"] == "pending"
                         else f"{how}: {short(s.get('summary') or '', 160)}".rstrip(": "))
        for q in task["questions"]:
            if not q.get("answer"):
                q["answer"] = f"({how})"
        task["previous_remaining"], task["remaining"] = task["remaining"], []
        for k in ("waiting", "paused", "replan"):
            task.pop(k, None)
        task["status"] = "cancelled" if cancel else "done"
        task["closed"] = {"at": now_iso(), "how": how, "note": note}
        task["decisions"].append({"text": f"Task {how}" + (f": {note}" if note else ""), "by": "user", "at": now_iso()})
        task["outcome"] = how
        self._record_lifecycle(task)
        self.store.save(task)
        self.store.event(task["id"], "closed", how=how, note=note)
        report = render_report(task, self.store.suggestions())
        with open(self.store.path(task["id"], "report.md"), "w", encoding="utf-8") as f:
            f.write(report)
        return {"task": task, "report": report}

    @staticmethod
    def _reopen_steps(task: Dict[str, Any]) -> None:
        """Before a task runs its own plan again: unfinished steps run again (keeping their sessions and history, so a
        cut-off one resumes); a step that asked questions runs again once they are all answered."""
        open_from = {q.get("from_step") for q in task["questions"] if not q.get("answer")}
        for s in task["steps"]:
            if s["status"] in ("failed", "blocked", "skipped", "paused") and not s.get("skipped_by_user"):
                s["status"], s["summary"] = "pending", ""
                s.pop("fresh_retry", None)
            elif s["status"] == "needs_input" and s["id"] not in open_from:
                s["status"] = "pending"
                if any(q.get("from_step") == s["id"] and q.get("answer") for q in task["questions"]):
                    s["followup"] = "answered"

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
        replies = _split_answers(text, len(open_q))
        home = self._home_project(task)
        for q, reply in zip(open_q, replies):
            q["answer"] = reply
            if q.get("kind") == "budget" and YES_RE.match(reply):
                task["budget_ack"] = now_iso()
            if q.get("kind") == "approval" and YES_RE.match(reply):
                task["plan_approved"] = now_iso()
            if q.get("kind") == "business":  # confirmed business rules outlive the task: keep them in the project
                proj = self.registry.projects.get(q.get("project") or "") or home
                if proj:
                    ProjectKnowledge(proj.path).add_decision(q["text"], reply, task["id"], task.get("feature", ""))
            step = next((s for s in task["steps"] if s["id"] == q.get("from_step")), None)
            if step and SKIP_RE.match(reply):
                step.update(status="skipped", skipped_by_user=True, summary=f"skipped by you: {short(q['text'], 120)}")
            task["decisions"].append({"text": f"User answered \"{short(q['text'], 160)}\": {reply}", "by": "user",
                                      "at": now_iso()})
        # Answers to a worker's questions go back to that worker's session and the plan carries on; plain approvals
        # also continue the plan. Anything else (the planner's own questions, feedback) needs a new plan.
        task["replan"] = not all(q.get("from_step") or (q.get("kind") in ("budget", "approval") and yes) for q in open_q)
        if not task["replan"]:
            self._reopen_steps(task)
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
    def accept_suggestion(self, s: Dict[str, Any], cwd: str, conversation: str = "",
                          confirm_plan: bool = False) -> Tuple[Optional[Dict[str, Any]], str]:
        """Act on an accepted suggestion: a task to run, or (for "git init") something Foreman does itself.
        Workers never run git; initialising a repository (no commit, nothing else) is the one git action the engine
        takes, and only because the user accepted this suggestion."""
        proj = self.registry.projects.get(s["project"])
        path = proj.path if proj else cwd
        if GIT_INIT_RE.search(s["text"]) and os.path.isdir(path):
            if os.path.isdir(os.path.join(path, ".git")):
                return None, f"{path} is already a git repository."
            res = run_process(["git", "init", "-q"], cwd=path, timeout=30)
            if res.code != 0:
                return None, f"✘ `git init` failed in {path}: {short(res.err or res.out, 200)}"
            if proj:
                self.registry.projects[proj.name] = self.registry._describe(proj.name, proj.path, proj)
                self.registry.save()
            return None, (f"✔ Ran `git init` in {path} (no commit was made). Foreman can now detect and review changes "
                          "there; commit when you like.")
        return self.prepare_new(s["text"], path, conversation, confirm_plan), ""

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
        if nxt.get("resume"):  # the work isn't finished: carry on with that task rather than opening a new one
            return self.prepare_resume(str(nxt["resume"]), cwd=cwd)
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

    def _next_step(self, task: Dict[str, Any], mode: Mode) -> Optional[Dict[str, Any]]:
        """What comes next, from the facts of the run (deterministic). An unfinished task is continued, never replaced
        by a new one ("resume": its plan, sessions and history carry on); a finished one moves to the next mode."""
        if task["status"] in ("cancelled", "failed", "needs_input"):
            return None  # nothing to recommend: the report shows the error or the questions
        if task["status"] in ("interrupted", "paused", "blocked", "partial"):
            left = [s for s in task["steps"] if not _settled(s)]
            if task["status"] == "paused":
                why = f"carry on once a provider is available ({short((task.get('paused') or {}).get('reason', ''), 120)})" \
                    if task.get("paused") else "carry on once a provider is available"
            elif task["status"] == "partial":
                why = "fix the problems the last checks or review still found"
            elif left:
                why = f"{len(left)} step{'s' if len(left) != 1 else ''} left, starting with {left[0]['id']} in " \
                      f"{left[0]['project']}"
            else:
                why = "finish what was stopped"
            return {"mode": mode.name, "reason": why, "resume": task["id"]}
        if task["remaining"] and mode.name == "qa":
            return {"mode": "development", "reason": "fix what QA found"}
        if not mode.next:
            return None
        # A finished research / planning / deployment step moves on; open items it found are for the user to settle first.
        reason, left = NEXT_REASONS.get(mode.next, ""), len(task["remaining"])
        return {"mode": mode.next, "reason": reason + (f" (settle the {left} open item{'s' if left != 1 else ''} first)" if left else "")}

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
        task.pop("waiting", None)
        task.pop("paused", None)
        task["next_buffer"] = []
        for s in task["steps"]:
            if s["status"] in ("running", "paused"):  # an interrupted run, or one that waited out provider limits
                s["status"] = "pending"
            for a in s.get("history") or []:
                if not a.get("ended"):  # this process (or the machine) died mid-attempt: its session is resumable
                    a.update(ended=now_iso(), outcome="interrupted")
            if s["status"] == "pending":  # a new run gets fresh retry allowances
                for k in ("paused", "limit_checks", "transient", "format_retry"):
                    s.pop(k, None)
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
        if any(s["status"] == "paused" for s in task["steps"]):
            task["status"] = "paused"  # every provider is out of its usage limit; "continue" picks it up
            task["paused"] = next(s["paused"] for s in task["steps"] if s.get("paused"))
            return
        if any(not _settled(s) for s in task["steps"]):
            # A step could not finish (and the steps waiting for it didn't run). A half-done plan is not verified,
            # fixed or written up; "continue" retries from here, in the same sessions.
            task["status"] = "blocked"
            return
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

    def side_question(self, question: str, cwd: str, conversation: str = "", running: Optional[str] = None) -> str:
        """/btw: answer a quick question on the side, read-only, without a task (nothing is recorded or changed, and a
        running task is left alone). Returns the answer, or one line saying why there is none."""
        sel = self.registry.select(question, cwd)
        names = {p.name for p in sel.focus} | {p.name for _, p in sel.neighbors}
        history = "\n".join(f"- #{t['id']} [{t['status']}] {t['title']}" for t in self.store.all()[:30]
                            if names & set(t["projects"]))[:2000]
        task = self.store.load(running) if running else None
        now = f"#{task['id']} {task['title']} ({task['status']}): " + "; ".join(
            f"[{s['status']}] {s['project']}: {short(s.get('summary') or s['instructions'], 100)}" for s in task["steps"]) \
            if task else ""
        anchor = sel.anchor.path if sel.anchor else (sel.focus[0].path if sel.focus else cwd)
        try:
            data = self._brain().call(Brain.btw_prompt(question, cwd, self.registry.render_context(sel), history, now,
                                                       conversation), BTW_SCHEMA, anchor,
                                      extra_dirs=[p.path for p in sel.focus if p.path != anchor][:4], purpose="plan")
        except BrainError as e:
            return f"I couldn't answer that on the side right now ({short(str(e), 160)})."
        return clean_text(str(data.get("answer") or "").strip()) or "No answer came back."

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
                                   knowledge_ctx=knowledge, style=self.cfg.output_style)
        cwd = sel.anchor.path if sel.anchor else (sel.focus[0].path if sel.focus else task["cwd"])
        extra = [p.path for p in sel.focus if p.path != cwd][:6] + image_dirs(task["request"])
        self._say(task, "planning…")
        before = snapshot(cwd)
        try:
            plan = self._think(task, lambda: self._brain(task).call(prompt, PLAN_SCHEMA, cwd, extra, purpose="plan",
                                                                    web=bool(fixed and modes[fixed].web)))
        except BrainError as e:
            if task.get("paused"):  # every provider is out of its usage limit: nothing is wrong with the task
                task["status"] = "paused"
            else:
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
            plan = self._think(task, lambda: self._brain(task).call(
                Brain.replan_prompt(task, discoveries, self.registry.render_context(sel)), PLAN_SCHEMA,
                sel.anchor.path if sel.anchor else task["cwd"], purpose="replan"))
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
            if plan.get("intent") == "answer":
                task["next_buffer"] = list(plan.get("next_for_you") or [])
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
                "expected_min": max(0, min(int(s.get("expected_minutes") or 0), 600)),
                "weight": "light" if s.get("weight") == "light" else "standard",
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
                task["questions"].append({"text": q["text"], "kind": "business", **question_options(q), "answer": None})
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
            "weight": "standard",
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
                    # A step whose dependency didn't finish stays pending (waiting for it): "continue" runs it once the
                    # dependency is done, instead of it being lost as skipped.
                    deps = [by_id.get(d) for d in s["depends_on"]]
                    if all(d is None or _settled(d) for d in deps) and s["project"] not in busy_projects \
                            and len(running) < self.cfg.limits.max_parallel:
                        s["status"] = "running"
                        busy_projects.add(s["project"])
                        running[pool.submit(self._run_step, task, s)] = s
                with self._lock:  # workers write to the task too (live session ids)
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
        """Run one step to a result, whatever happens to the providers on the way. Every attempt is journaled in
        step["history"] and its session id is saved the moment the CLI reveals it, so nothing is lost when a run is cut
        off. Policy by failure kind (providers.base): a timeout or a network error resumes the same session; a missing
        final report is asked for in the same session; a usage limit or a login problem rests that provider and hands
        the step to the next one with a handoff brief; when every provider is out of limits, Foreman waits for the
        reset (checking every few minutes) and then carries on; a crash retries once fresh, then moves on."""
        proj = self.registry.projects[step["project"]]
        before = snapshot(proj.path)
        head_before = git_head(proj.path)
        self._step_snapshot(task, step, before)
        prompt = self._step_prompt(task, step, proj)
        lim = self.cfg.limits
        tried: List[str] = []    # providers given up on for this step in this run
        limited: List[str] = []  # ... of those, the ones out of usage limits (worth waiting for)
        result: Optional[RunResult] = None
        only: Optional[str] = None  # auto-switch off: after a usage limit this provider is the only one to use
        while not STOPPING.is_set():
            if not self.cfg.auto_switch and not only:
                pref = self._preferred(step)
                if pref and pref not in tried and self._rested_for_limit(pref):  # don't quietly use another one
                    if self._wait_for_limits(task, step, [pref]):
                        continue
                    break
            choice = self._choose(task, step, proj.path, tried + [n for n in self.providers if only and n != only])
            if not choice:
                if limited and self._wait_for_limits(task, step, limited):
                    tried = [n for n in tried if n not in limited]
                    limited.clear()
                    continue
                break
            provider, session_id, fork, why = choice
            ok, warn = self.budget.acquire(provider.name)
            if not ok:  # Foreman's own daily cap behaves like a usage limit that lifts at midnight
                self._notice(task, warn)
                self._provider_failed(provider.name, LIMIT, warn, _midnight())
                tried.append(provider.name)
                limited.append(provider.name)
                continue
            if warn:
                self._notice(task, warn)
            step["attempts"] += 1
            timeout = self._step_timeout(step)
            attempt_prompt, why = self._attempt_prompt(task, step, proj, prompt, session_id, fork, timeout, why)
            notes = self._notes_path(task, step) if step["kind"] == "implement" and \
                provider.capabilities.writes_extra_dirs else None
            if notes:
                attempt_prompt += (f"\n\nPROGRESS NOTES: after each milestone, append one line to {notes} (what is done, "
                                   "what is next). This file is outside the project and is Foreman's; if your run is cut "
                                   "off, the next worker continues from it.")
            self._say(task, f"{step['kind']} {step['project']} via {provider.name}" + (f" ({why})" if why else ""))
            pick = self._pick(provider.name, "light" if step.get("weight") == "light" else "standard")
            self.store.event(task["id"], "step_start", step=step["id"], provider=provider.name, project=step["project"],
                             step_kind=step["kind"], session=session_id, fork=fork, why=why, model=pick.model,
                             effort=pick.effort, weight=step.get("weight"))
            attempt = {"provider": provider.name, "session": None if fork else session_id, "started": now_iso(),
                       "why": why, "model": pick.model, "effort": pick.effort}
            with self._lock:
                step.setdefault("history", []).append(attempt)
                self.store.save(task)
            mode = self._mode(task)
            result = provider.run(RunRequest(
                web=bool(mode and mode.web and step["kind"] == "investigate"),
                prompt=attempt_prompt, cwd=proj.path, mode="write" if step["kind"] == "implement" else "read",
                session_id=session_id, fork=fork, schema=STEP_SCHEMA, timeout=timeout, model=pick.model,
                effort=pick.effort,
                extra_dirs=[self.registry.projects[r].path for r in step["read_projects"] if r in self.registry.projects]
                + image_dirs(task["request"]) + ([os.path.dirname(notes)] if notes else []),
                visual=self.visual, title=f"Foreman #{task['id']} · {step['kind']} · {step['project']}",
                on_activity=lambda text, _p=provider.name: self._activity(task["id"], _p, step["project"], text),
                on_session=lambda sid, _p=provider.name, _a=attempt, _f=(session_id if fork else None):
                    self._live_session(task, step, _p, sid, _a, _f)))
            self.store.write_log(task["id"], f"step-{step['id']}-{provider.name}", result.raw or result.text or str(result.error))
            self._note_usage(provider.name, result.usage)
            kind = "" if result.ok else (STOPPED if STOPPING.is_set() else result.kind)
            sid = result.session_id or attempt.get("session") or (None if fork else session_id)
            with self._lock:
                attempt.update(ended=now_iso(), outcome="done" if result.ok else kind, session=sid,
                               error=short(result.error or "", 300) or None)
                if sid and not any(s["session_id"] == sid for s in task["sessions"]):
                    task["sessions"].append({"provider": provider.name, "session_id": sid, "project": step["project"],
                                             "step": step["id"], "forked_from": session_id if fork else None,
                                             "at": now_iso()})
            step.pop("followup", None)
            if result.ok:
                step["provider"], step["session_id"] = provider.name, sid
                break
            if kind == STOPPED:
                break  # the user stopped the task: not a provider failure
            self._provider_failed(provider.name, kind, result.error or "", result.retry_at)
            self.store.event(task["id"], "step_failed", step=step["id"], provider=provider.name, error=result.error,
                             cause=kind)
            if kind == TIMEOUT and step.get("timeouts", 0) < lim.timeout_retries:
                # Running out of time is not a stale session: resume the same one (same provider) with more time.
                step["timeouts"] = step.get("timeouts", 0) + 1
                self._keep_session(step, provider.name, sid, "timeout")
                more = self._step_timeout(step)
                self.store.event(task["id"], "step_timeout", step=step["id"], provider=provider.name, session=sid,
                                 next_timeout=more)
                self._notice(task, f"{provider.name} ran out of time on {step['project']} ({step['id']}); continuing "
                                   f"{'the same session' if sid else 'in a new session (none to resume)'} with up to "
                                   f"{more // 60} min")
                continue
            if kind == TRANSIENT and step.get("transient", 0) < lim.transient_retries:
                wait = TRANSIENT_WAITS[min(step.get("transient", 0), len(TRANSIENT_WAITS) - 1)]
                step["transient"] = step.get("transient", 0) + 1
                self._keep_session(step, provider.name, sid, "interrupted")
                self.store.event(task["id"], "step_retry", step=step["id"], provider=provider.name, wait=wait,
                                 reason=short(result.error or "", 160))
                self._notice(task, f"{provider.name} hit a network/API error on {step['project']}; resumed its session "
                                   f"after {wait}s")
                if STOPPING.wait(wait):
                    break
                continue
            if kind == OUTPUT and sid and not step.get("format_retry"):
                step["format_retry"] = True  # the work is probably done; only the final report is missing
                self._keep_session(step, provider.name, sid, "format")
                continue
            if kind == LIMIT:
                limited.append(provider.name)
                if not self.cfg.auto_switch:
                    only = provider.name
                until = f" (resets {clock(result.retry_at)})" if result.retry_at else ""
                self._notice(task, f"{provider.name} reached its usage limit{until} during {step['project']}; "
                                   + ("another provider takes over from where it stopped" if self.cfg.auto_switch else
                                      "waiting for it (automatic switching is off)"))
            elif kind == AUTH:
                self._notice(task, f"{provider.name} can't log in ({short(result.error or '', 120)}); renew its key. "
                                   "Another provider takes over.")
            elif kind == CONTEXT and provider.name not in step.setdefault("fresh_retry", []):
                step["fresh_retry"].append(provider.name)  # same provider, fresh session, with a handoff brief
                step["session_id"], step["no_reuse"] = None, True
                self._notice(task, f"{provider.name}'s session for {step['project']} is full; continuing in a fresh "
                                   "session with a handoff")
                continue
            else:
                self._notice(task, f"{provider.name} failed on {step['project']}: {short(result.error or '', 160)}")
                # A failed resume may be a stale session: retry the same provider fresh before moving on (a timeout is
                # not that; once its retries are used up the step moves on to the next provider).
                if session_id and kind != TIMEOUT and provider.name not in step.setdefault("fresh_retry", []):
                    step["fresh_retry"].append(provider.name)
                    step["session_id"], step["no_reuse"] = None, True
                    continue
            tried.append(provider.name)
        with self._lock:
            self._record_step(task, step, proj, result, before, head_before)

    @staticmethod
    def _keep_session(step: Dict[str, Any], provider: str, sid: Optional[str], followup: str) -> None:
        """The next attempt continues this session (same provider) and is told why it was cut off."""
        if sid:
            step["provider"], step["session_id"] = provider, sid
            step.pop("no_reuse", None)
        step["followup"] = followup

    def _live_session(self, task, step, provider: str, sid: str, attempt: Dict[str, Any],
                      forked_from: Optional[str]) -> None:
        """A run revealed its session id: record it now, so it can be resumed even if this process dies."""
        with self._lock:
            attempt["session"] = sid
            step["provider"], step["session_id"] = provider, sid
            step.pop("no_reuse", None)
            if not any(s["session_id"] == sid for s in task["sessions"]):
                task["sessions"].append({"provider": provider, "session_id": sid, "project": step["project"],
                                         "step": step["id"], "forked_from": forked_from, "at": now_iso()})
            self.store.save(task)

    def _attempt_prompt(self, task, step, proj, prompt: str, session_id: Optional[str], fork: bool, timeout: int,
                        why: str) -> Tuple[str, str]:
        """The prompt for this attempt. Back in the session that was cut off: why it stopped and to carry on. In a
        different session than the step's last one: the full step plus a handoff brief of what was done so far."""
        history = step.get("history") or []
        last = history[-1] if history else None
        same = bool(session_id) and not fork and bool(last) and last.get("session") == session_id
        follow = step.get("followup")
        if same and follow == "format":
            return ("Your previous run of this step finished without the required final result. Do not redo the work: "
                    "check its state briefly and report the result of this step now, in the required format."), \
                "asking for the result it didn't report"
        if same and follow == "timeout":
            return self._continue_prompt(timeout), "continuing the same session after a timeout"
        answers = [q for q in task["questions"] if q.get("from_step") == step["id"] and q.get("answer")]
        if same and follow == "answered" and answers:
            return ("The user answered the questions you returned:\n"
                    + "\n".join(f"- Q: {short(q['text'], 300)}\n  A: {q['answer']}" for q in answers)
                    + f"\nContinue this step in this session with these answers and finish it. You have about "
                      f"{timeout // 60} minutes; report the final result in the required format."), \
                "continuing with your answers"
        if same and last.get("outcome") not in (None, "done"):
            reason = STOP_REASONS.get(last.get("outcome"), "an error")
            return (f"Your previous run of this step was cut off by {reason} before it reported. Continue the SAME step "
                    "in this session: check what is already done on disk and in your background jobs, do not redo "
                    f"finished work or restart downloads, and finish what is left. You have about {timeout // 60} "
                    "minutes; report the final result in the required format."), f"resuming its session after {reason}"
        if history and not same:
            prev = next((a["provider"] for a in reversed(history)), "")
            return prompt + "\n\n" + self._handoff(task, step, proj, timeout), \
                why or f"taking over from {prev}" if prev else why
        return prompt, why

    @staticmethod
    def _continue_prompt(timeout: int) -> str:
        return ("Foreman's time limit cut your previous run of this step short before you reported a result. Continue "
                "the SAME step in this session: check what already exists on disk and in your background jobs, do not "
                "redo finished work or restart downloads, and finish what is left. "
                f"You now have about {timeout // 60} minutes. Keep long jobs in the background and poll them "
                "briefly, and report the final result in the required format as soon as the step is done.")

    def _handoff(self, task, step, proj, timeout: int) -> str:
        """What a new session needs to take over a started step: who worked on it and why they stopped, the files
        changed since the step started, the previous worker's progress notes and its last actions."""
        history = step.get("history") or []
        lines = ["HANDOFF: this step was already started in another session and is not finished. Take it over; do not "
                 "start from scratch."]
        for a in history[-5:]:
            outcome = a.get("outcome")
            state = "stopped for your input (see USER ANSWERS)" if outcome == "done" else \
                f"cut off by {STOP_REASONS.get(outcome or 'interrupted', 'an error')}"
            lines.append(f"- {a['provider']} from {str(a.get('started', ''))[11:16]}: {state}"
                         + (f" ({short(a['error'], 140)})" if a.get("error") and outcome != "done" else ""))
        if step.get("summary") and history and history[-1].get("outcome") == "done":
            lines.append(f"Its report: {short(step['summary'], 600)}")
        base = read_json(self._snapshot_path(task, step), None)
        files = changed_since(proj.path, base) if base else []
        if files:
            lines.append(f"Files changed in {proj.name} since this step started ({len(files)}): " + ", ".join(files[:40])
                         + (" …" if len(files) > 40 else ""))
        notes = _tail(self._notes_path(task, step), 3000)
        if notes:
            lines.append(f"The previous worker's progress notes ({self._notes_path(task, step)}):\n{notes}")
        started = history[0].get("started", "") if history else ""
        acts = [e["text"] for e in self.store.events(task["id"]) if e.get("event") == "activity"
                and e.get("where") == step["project"] and str(e.get("at", "")) >= started][-15:]
        if acts:
            lines.append("Its last actions:\n" + "\n".join(f"  {short(a, 160)}" for a in acts))
        lines.append("First verify on disk what is already done (the files above, background jobs, logs), then finish the "
                     f"rest. Do not redo finished work or restart completed downloads. You have about {timeout // 60} "
                     "minutes.")
        return "\n".join(lines)

    def _snapshot_path(self, task, step) -> str:
        return self.store.path(task["id"], "snapshots", re.sub(r"[^\w.-]", "_", step["id"]) + ".json")

    def _notes_path(self, task, step) -> str:
        return self.store.path(task["id"], "notes", re.sub(r"[^\w.-]", "_", step["id"]) + ".md")

    def _step_snapshot(self, task, step, snap: Dict[str, Any]) -> None:
        """The project as it was when the step first started (kept across runs, for handoffs)."""
        path = self._snapshot_path(task, step)
        if not os.path.exists(path):
            write_json(path, snap)
        os.makedirs(os.path.dirname(self._notes_path(task, step)), exist_ok=True)

    def _wait_for_limits(self, task, holder: Dict[str, Any], limited: List[str]) -> bool:
        """Every usable provider is out of its usage limit. Wait for the earliest one to be usable again (its stated
        reset, or the next periodic check) and return True to try again; False to give up: the checks are used up,
        the reset is too far away, or the user stopped the task. holder (a step or a dict) keeps the check count."""
        lim = self.cfg.limits
        while not STOPPING.is_set():
            health, now = self._health(), time.time()
            entries = [health.get(n) or {} for n in limited]
            until = min([e.get("until", 0) for e in entries] or [0])
            known = any(e.get("known") and e.get("until", 0) == until for e in entries)
            checks = holder.get("limit_checks", 0)
            names = ", ".join(f"{n}" + (f" (resets {clock(e['until'])})" if e.get("known") else "")
                              for n, e in zip(limited, entries))
            if checks >= lim.limit_retries or (known and until - now > lim.limit_wait_max_hours * 3600):
                holder["paused"] = {"reason": f"all providers are out of their usage limits: {names}",
                                    "until": until if known else None}
                self._notice(task, f"Paused: {holder['paused']['reason']}. Say \"continue\" when one is available.")
                return False
            if until <= now:
                holder["limit_checks"] = checks + 1
                return True
            with self._lock:
                task["waiting"] = {"until": until, "reason": f"waiting for provider limits: {names}",
                                   "check": checks + 1, "of": lim.limit_retries, "known": known}
                self.store.save(task)
            self.store.event(task["id"], "waiting", until=until, reason=task["waiting"]["reason"], check=checks + 1,
                             of=lim.limit_retries)
            self._say(task, f"{task['waiting']['reason']}; next try at {clock(until)}")
            # Sleep in short slices: another provider may become usable sooner (key renewed, daily cap reset).
            while time.time() < until and not STOPPING.is_set():
                if STOPPING.wait(min(30.0, max(0.05, until - time.time()))):
                    break
                if self.cfg.auto_switch and any(self._usable(n) for n in self.providers if n not in limited):
                    break
            with self._lock:
                task.pop("waiting", None)
            if STOPPING.is_set():
                return False
            if not known:
                holder["limit_checks"] = checks + 1
            return True
        return False

    def _step_timeout(self, step: Dict[str, Any]) -> int:
        """Seconds for the step's next attempt: the configured time, or about 1.5x what the planner expects for a long
        step, doubled for every timeout so far, never above the configured ceiling."""
        lim = self.cfg.limits
        base = max(lim.step_timeout_sec, min(int(step.get("expected_min") or 0) * 90, lim.step_timeout_max_sec))
        return int(min(base * 2 ** step.get("timeouts", 0), max(lim.step_timeout_max_sec, base)))

    def _record_step(self, task, step, proj, result, before, head_before) -> None:
        if STOPPING.is_set() and not (result and result.ok):
            step["status"], step["summary"] = "pending", "stopped by you before it finished; continue resumes it"
            return
        if step.get("paused") and not (result and result.ok):
            step["status"] = "paused"
            step["summary"] = step["paused"]["reason"]
            self.store.event(task["id"], "step_done", step=step["id"], project=step["project"], status="paused",
                             summary=step["summary"])
            return
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
        # Every question a worker asks goes to the user (business or technical), with the worker's options and its
        # recommendation first. A step never waits for input without a question the user can see and answer.
        asked = []
        for q in data.get("questions", []) or []:
            if not str(q.get("text") or "").strip():
                continue
            asked.append({"text": q["text"].strip(), "kind": q.get("kind") or "technical", **question_options(q),
                          "answer": None, "from_step": step["id"], "project": step["project"]})
        if not asked and step["status"] == "needs_input":
            asked.append({"text": f"The worker in {proj.name} stopped and needs your input:\n{short(step['summary'], 900)}",
                          "kind": "technical", "options": [], "default": None, "answer": None, "from_step": step["id"],
                          "project": step["project"]})
        if not asked and step["status"] == "blocked":
            asked.append({"text": f"The worker in {proj.name} is blocked:\n{short(step['summary'], 900)}\n"
                                  "How should it continue?", "kind": "blocker",
                          "options": ["Try again", "Skip this step"], "default": "Try again", "answer": None,
                          "from_step": step["id"], "project": step["project"]})
        for q in asked:
            if not any(x["text"] == q["text"] and not x.get("answer") for x in task["questions"]):
                task["questions"].append(q)
        if asked:
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
        if step["status"] == "done":
            task.setdefault("next_buffer", []).extend(data.get("next_for_you") or [])

    def _rested_for_limit(self, name: str) -> bool:
        h = self._health().get(name) or {}
        return h.get("kind") == LIMIT and h.get("until", 0) > time.time()

    def _preferred(self, step: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """The provider Foreman would use first for a step: the one holding its session, else the first one in the
        preference order that is installed and can log in (one resting for a usage limit still counts)."""
        if step and step.get("session_id") and step.get("provider") in self.providers:
            return step["provider"]
        for name, p in self.providers.items():
            if p.available()[0] and (name not in self.unhealthy() or self._rested_for_limit(name)):
                return name
        return None

    def _pick(self, provider: str, role: str) -> Choice:
        """Model and effort for one run: what the user chose for that provider (`foreman models`, or the built-in
        default). Light steps and planning (role "light" / "plan") run one effort level lower; "standard" and "review"
        use it as chosen. Providers without a choice use their CLI default."""
        settings = self.cfg.providers.get(provider)
        return settings.choice(role) if settings else Choice()

    def _choose(self, task, step, cwd, tried) -> Optional[Tuple[Provider, Optional[str], bool, str]]:
        """Pick provider + session. Continuity first, then forking a recent user session, then a new session."""
        avail = [p for n, p in self.providers.items() if n not in tried and self._usable(n)]
        if not avail:
            return None
        # A provider near its usage limit keeps the sessions it has, but new work goes to the others first.
        if self.cfg.auto_switch:
            avail = [p for p in avail if not self._pressed(p.name)] + [p for p in avail if self._pressed(p.name)]
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
            "- Routine technical choices are yours. A choice that changes what was asked (a new dependency, dropping a "
            "source or feature, deviating from the plan) is the user's: stop and return it as a question of kind "
            "'technical' with the options and your recommended one as 'default'. The user is asked and you continue "
            "in this session with the answer.\n"
            if step["kind"] == "implement" else
            "- Read-only step: do not create, modify or delete files.\n"
            "- Answer precisely with file references; stop as soon as you have enough evidence.\n")
        rules += "- " + QUESTION_RULE.replace("\n", " ") + "\n"
        rules += (f"- Time: about {self._step_timeout(step) // 60} minutes for this step. Start anything long (downloads, big "
                  "scans, builds) in the background (nohup/setsid, output to a log file) and poll it with short sleeps "
                  "instead of one blocking command, and keep notes on disk of what is done. If time runs out, Foreman "
                  "resumes this same session.\n")
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
            "- Keep the summary short and factual. In 'next_for_you' put 0-3 short, concrete things the user could do or "
            "ask next, given what you did and the project's state (leave it empty if nothing useful follows).",
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
                task["status"] = "done"
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
            if any(s["status"] == "paused" for s in task["steps"]):
                task["status"] = "paused"
                task["paused"] = next(s["paused"] for s in task["steps"] if s.get("paused"))
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
            self.store.event(task["id"], "check", project=c["project"], command=c["command"], ok=ok,
                             **({} if ok else {"tail": "\n".join(tail.splitlines()[-40:])}))
            results.append({**c, "ok": ok, "tail": tail})
        return results

    def _review(self, task, changed, checks) -> Optional[Dict[str, Any]]:
        diffs = {n: diff_text(task["projects"][n]["path"], files) for n, files in changed.items()}
        implementers = {s["provider"] for s in task["steps"] if s["kind"] == "implement" and s.get("provider")}
        names = list(changed)
        cwd = task["projects"][names[0]]["path"]
        extra = [task["projects"][n]["path"] for n in names[1:]]
        try:
            knowledge = "\n".join(k for k in (ProjectKnowledge(task["projects"][n]["path"]).context(n) for n in names) if k)
            review = self._think(task, lambda: self._brain(task, prefer_not=implementers).call(
                Brain.review_prompt(task, diffs, checks, knowledge[:4000]), REVIEW_SCHEMA, cwd, extra, purpose="review"))
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
                "weight": prev.get("weight", "standard") if prev else "standard",
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
                data = self._think(task, lambda: self._brain(task).call(
                    Brain.document_prompt(task, mode.title, mode.section("output"), findings or "(none)", checks,
                                         self.cfg.output_style),
                    DOC_SCHEMA, home.path, [e["path"] for n, e in task["projects"].items() if n != home.name][:5],
                    purpose="document"))
                self._provider_errors(task, data)
                text, summary = clean_text(data.get("document", "")), clean_text(data.get("summary", ""))
                task["remaining"] += [short(p, 200) for p in data.get("open_problems") or [] if p.strip()][:8]
                task.setdefault("next_buffer", []).extend(data.get("next_for_you") or [])
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
        if task["status"] in ("blocked", "partial", "failed", "paused", "interrupted"):
            by_id = {s["id"]: s for s in task["steps"]}
            for s in task["steps"]:
                if _settled(s):
                    continue
                waits = [d for d in s["depends_on"] if d in by_id and not _settled(by_id[d])]
                why = f"waiting for {', '.join(waits)}" if waits and s["status"] == "pending" else \
                    s["summary"] or s["instructions"][:160]
                task["remaining"].append(f"{s['project']} ({s['kind']}, {s['id']}): {short(why, 200)}")
        task["remaining"] = list(dict.fromkeys(task["remaining"]))[-12:]
        for name, entry in task["projects"].items():
            if os.path.isdir(entry["path"]):
                entry["after"] = snapshot(entry["path"])
        mine = [" ".join(str(x).split()) for x in task.pop("next_buffer", []) if str(x).strip()]
        task["next_for_you"] = [short(x, 120) for x in dict.fromkeys(mine)][:3] \
            if task["status"] in ("done", "answered", "partial") else []
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
        task["next"] = self._next_step(task, mode) if mode and task["status"] != "cancelled" else None
        if task["next"]:
            nxt = self.modes(task["cwd"]).get(task["next"]["mode"])
            task["next"]["title"] = f"Continue #{task['id']}" if task["next"].get("resume") else \
                (nxt.title if nxt else task["next"]["mode"])
        if not mode or task["status"] in ("interrupted", "cancelled"):
            return
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
        if self.cfg.auto_switch:
            names = [n for n in names if not self._pressed(n)] + [n for n in names if self._pressed(n)]
        provs = [self.providers[n] for n in names if self._usable(n)]
        tid = task["id"] if task else None
        return Brain(provs, timeout=self.cfg.limits.brain_timeout_sec, stop_on_limit=not self.cfg.auto_switch,
                     choice_for=lambda name, purpose: self._pick(name, "review" if purpose == "review" else "plan"),
                     on_call=lambda name: self.budget.acquire(name)[0],
                     on_start=lambda name, purpose: tid and self.store.event(tid, "thinking", provider=name, purpose=purpose),
                     on_activity=(lambda name, purpose, text: self._activity(tid, name, purpose, text)) if tid else None,
                     on_fail=lambda name, err: self._brain_failed(tid, name, err))

    # ------------------------------------------------------------------ provider health
    def _health(self) -> Dict[str, Any]:
        return read_json(os.path.join(self.cfg.home, "health.json"), {}) or {}

    def unhealthy(self) -> Dict[str, str]:
        """Providers resting because they can't work right now (login failed, usage limit): name -> reason."""
        now = time.time()
        return {n: h["error"] + (f" (until {clock(h['until'])})" if h.get("kind") == LIMIT and h.get("known") else "")
                for n, h in self._health().items() if not n.startswith("_") and h.get("until", 0) > now}

    def _usable(self, name: str) -> bool:
        p = self.providers.get(name)
        return bool(p and p.available()[0] and name not in self.unhealthy())

    def _provider_failed(self, name: str, kind: str, error: str, retry_at: Optional[float] = None) -> None:
        """Rest a provider that can't work right now: a login or billing problem for 6 h; a usage limit until its stated reset, or
        until the next periodic check when it didn't say. Other failures don't make a provider unusable."""
        now = time.time()
        if kind == AUTH:
            until, known = now + 6 * 3600, True
        elif kind == LIMIT:
            until, known = (retry_at, True) if retry_at and retry_at > now else (now + self.cfg.limits.limit_retry_sec, False)
        else:
            return
        with self._lock:
            h = self._health()
            h[name] = {"error": short(error, 160), "kind": kind, "at": now_iso(), "until": until, "known": known}
            write_json(os.path.join(self.cfg.home, "health.json"), h)

    def _note_usage(self, name: str, usage: Optional[Dict[str, Any]]) -> None:
        """Remember how close a provider is to its usage limit (providers that report it), for _pressed."""
        if not usage:
            return
        with self._lock:
            h = self._health()
            h.setdefault("_usage", {})[name] = usage
            write_json(os.path.join(self.cfg.home, "health.json"), h)

    def _pressed(self, name: str) -> bool:
        """The provider said it is near its usage limit (at or above pressure_percent) and the window hasn't reset."""
        u = (self._health().get("_usage") or {}).get(name) or {}
        return u.get("resets_at", 0) > time.time() and \
            float(u.get("utilization") or 0) * 100 >= self.cfg.limits.pressure_percent

    def _brain_failed(self, task_id: Optional[str], name: str, result: RunResult) -> None:
        self._provider_failed(name, result.kind, result.error or "", result.retry_at)
        self._note_usage(name, result.usage)
        if task_id:
            self.store.event(task_id, "thinking_failed", provider=name, error=short(result.error or "", 160),
                             cause=result.kind)

    def _think(self, task: Dict[str, Any], call: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
        """A brain call (plan, review, document) that waits like a step does when every provider is out of its usage
        limit, instead of failing. Other errors are raised as they are."""
        holder: Dict[str, Any] = {}
        if not self.cfg.auto_switch:  # the planner's provider is resting for a limit: wait for it, don't switch
            names = list(self.providers)
            if self.cfg.brain in names:
                names.remove(self.cfg.brain)
                names.insert(0, self.cfg.brain)
            pref = next((n for n in names if self.providers[n].available()[0]), None)
            if pref and self._rested_for_limit(pref):
                self._wait_for_limits(task, holder, [pref])
        while True:
            try:
                return call()
            except BrainError as e:
                limited = [n for n, h in self._health().items() if not n.startswith("_") and n in self.providers
                           and h.get("kind") == LIMIT and h.get("until", 0) > time.time() - 5]
                if not limited or any(k not in (LIMIT, AUTH) for k in e.kinds) or \
                        not self._wait_for_limits(task, holder, limited):
                    if holder.get("paused"):
                        task["paused"] = holder["paused"]
                    raise

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


def _midnight() -> float:
    """Next local midnight (when Foreman's daily run counters reset)."""
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday + 1, 0, 0, 5, 0, 0, -1))


def _tail(path: str, limit: int) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    return text[-limit:].strip()


def _settled(step: Dict[str, Any]) -> bool:
    """Nothing is left to do for this step: done, or skipped because the user said so."""
    return step["status"] == "done" or bool(step.get("skipped_by_user"))


def _instructions(note: str) -> str:
    """What a "continue" message says beyond continuing ("continue, but use the CSV dumps" -> "but use the CSV
    dumps"); "" for a plain "continue" / "resume #26" / "continue the previous task"."""
    rest = RESUME_RE.sub("", note or "")
    rest = re.sub(r"#\d+|\b(the|a|it|this|that|previous|last|task|work|please|now|again|from|where|you|we|i|left|"
                  r"off|stopped|with|on|my|our|one|ok(ay)?|so|then)\b", " ", rest, flags=re.I)
    words = re.findall(r"[^\W_]{2,}", rest)
    return " ".join(words) if len(words) > 2 else ""


def _split_answers(text: str, count: int) -> List[str]:
    """One answer per open question. Numbered answers ("1. use pyarrow" / "2: no exemption", one per line) go to
    those questions; a single unnumbered answer goes to every question."""
    if count > 1:
        found: Dict[int, List[str]] = {}
        current = None
        for line in text.splitlines():
            m = ANSWER_ITEM_RE.match(line)
            if m and 1 <= int(m.group(1)) <= count:
                current = int(m.group(1))
                found[current] = [line[m.end():].strip()]
            elif current is not None and line.strip():
                found[current].append(line.strip())
        if found:
            return [" ".join(found[i]) if i in found else text.strip() for i in range(1, count + 1)]
    return [text.strip()] * count
