"""Concise reports, generated deterministically from the task record (never from AI free text alone)."""

import re
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional

from orchestrator.util import short

ICON = {"done": "✔", "answered": "✔", "partial": "◐", "needs_input": "?", "blocked": "✘", "failed": "✘",
        "running": "…", "planning": "…", "queued": "…", "interrupted": "!", "planned": "◌",
        "paused": "‖"}


def clock(ts: Optional[float]) -> str:
    """Epoch seconds as local time: "19:10" today, "Tue 19:10" within a week, else "2026-11-03 19:10"."""
    if not ts:
        return "?"
    t, now = time.localtime(ts), time.localtime()
    if t.tm_yday == now.tm_yday and t.tm_year == now.tm_year:
        return time.strftime("%H:%M", t)
    if 0 < ts - time.time() < 6 * 86400:
        return time.strftime("%a %H:%M", t)
    return time.strftime("%Y-%m-%d %H:%M", t)


def notify(title: str, body: str) -> None:
    """One desktop notification. Silently does nothing where unsupported."""
    exe = shutil.which("notify-send") or shutil.which("osascript")
    if not exe:
        return
    try:
        if exe.endswith("notify-send"):
            subprocess.Popen([exe, "-a", "Foreman", title, body], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen([exe, "-e", f'display notification "{body[:150]}" with title "{title}"'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def stopped_at(task: Dict[str, Any]) -> str:
    """Where an unfinished task stopped, from its step journal: "step s1 in api (claude: usage limit, its session
    is resumed)", or "" when no step is unfinished."""
    left = [s for s in task.get("steps") or [] if s["status"] != "done" and not s.get("skipped_by_user")]
    if not left:
        return ""
    first = next((s for s in left if s["status"] != "pending" or s.get("history")), left[0])
    hist = first.get("history") or []
    last = hist[-1] if hist else {}
    how = ""
    if last and last.get("outcome") not in (None, "done"):
        how = f"{last['provider']}: {OUTCOME.get(last['outcome'], last['outcome'])}"
        how += ", its session is resumed" if first.get("session_id") else ""
    more = len(left) - 1
    return (f"step {first['id']} in {first['project']}" + (f" ({how})" if how else "")
            + (f", {more} more step{'s' if more != 1 else ''} after it" if more > 0 else ""))


OUTCOME = {"timeout": "ran out of time", "limit": "usage limit", "transient": "network/API error", "auth": "login problem",
           "context": "context full", "output": "no final report", "crash": "stopped unexpectedly",
           "interrupted": "Foreman was closed", "stopped": "stopped by you"}


def option_labels(q: Dict[str, Any], mark: str = "recommended") -> List[str]:
    """A question's options, the worker's recommendation first and marked."""
    return [f"{o} ({mark})" if o == q.get("default") else o for o in q.get("options") or []]


def _projects_line(task: Dict[str, Any]) -> str:
    parts = []
    for name, e in task["projects"].items():
        files = e.get("changed_files") or []
        if e.get("role") == "modify" and "changed_files" not in e:
            parts.append(f"{name} (to change)")
        elif e.get("role") == "modify":
            parts.append(f"{name} (changed {len(files)} file{'s' if len(files) != 1 else ''})")
        else:
            parts.append(f"{name} ({ {'test': 'tested'}.get(e.get('role'), 'inspected') })")
    return " · ".join(parts)


def render_report(task: Dict[str, Any], all_suggestions: Optional[List[Dict[str, Any]]] = None,
                  questions: bool = True, chat: bool = False) -> str:
    st = task["status"]
    lines = [f"{ICON.get(st, '•')} #{task['id']} {task['title']} — {task.get('outcome') or st}"]
    if task.get("closed"):
        c = task["closed"]
        lines += ["", f"{c['how'].capitalize()} on {c['at'][:10]}" + (f": {c['note'].rstrip('.')}" if c.get("note") else "") + "."]
    if st in ("interrupted", "paused", "blocked", "partial"):
        where = stopped_at(task)
        go = "Say \"continue\" to pick it up there." if chat else f"`foreman resume {task['id']}` picks it up there."
        if st == "interrupted":
            lines += ["", f"Stopped by you{' at ' + where if where else ''}. {go}"]
        elif st == "paused":
            lines += ["", f"Paused{' at ' + where if where else ''}: {(task.get('paused') or {}).get('reason', 'no provider is available')}."
                      + (f" Expected back at {clock(task['paused']['until'])}." if (task.get("paused") or {}).get("until") else "")
                      + f" {go}"]
        elif where:
            lines += ["", f"Stopped at {where}. {go}"]

    if task["intent"] == "answer" and task.get("answer"):
        lines += ["", task["answer"].strip()]

    rows = []
    if task.get("mode"):
        how = "chosen by you" if task.get("mode_source") == "user" else \
            "recommended" + (f": {task['mode_reason']}" if task.get("mode_reason") else "")
        rows.append(("Mode", f"{task.get('mode_title') or task['mode']} ({how})"))
    if task["projects"]:
        rows.append(("Projects", _projects_line(task)))
    impact = task.get("impact") or {}
    if impact and task["intent"] != "answer":
        who = list(dict.fromkeys(short(re.split(r"[:(]", x)[0].strip(), 40) for x in
                                 (impact.get("consumers") or []) + (impact.get("tests") or []) if x))[:4]
        rows.append(("Impact", f"{impact.get('risk', '?')} risk: {short(impact.get('summary', ''), 120)}"
                     + (f" (affects {', '.join(who)})" if who else "")))
    if task.get("document"):
        d = task["document"]
        rows.append(("Document", f"{d['project']}/{d['path']}"))
    checks = {}
    for e in task["evidence"]:
        if e["kind"] == "check":
            checks[(e["project"], e["command"])] = e  # latest result per check
    if checks:
        rows.append(("Checks", "  ".join(f"{'✔' if e['ok'] else '✘'} {e['project']}: {e['command']}" for e in checks.values())))
    reviews = [e for e in task["evidence"] if e["kind"] == "review"]
    if reviews:
        rows.append(("Review", short(reviews[-1]["summary"], 160)))
    if task.get("fix_rounds"):
        rows.append(("Fixes", f"{task['fix_rounds']} automatic fix round(s) after validation"))
    used: Dict[str, int] = {}
    for st_ in task["steps"]:
        for a in st_.get("history") or []:
            if a.get("outcome") == "done" or a.get("ended") is None:
                key = f"{a['provider']} {a.get('model') or 'default'}" + (f" ({a['effort']})" if a.get("effort") else "")
                used[key] = used.get(key, 0) + 1
    if used:
        rows.append(("Models", ", ".join(f"{k} ×{n}" for k, n in used.items())))
    sessions = task.get("sessions") or []
    if sessions:
        by = {}
        for s in sessions:
            by.setdefault(s["provider"], 0)
            by[s["provider"]] += 1
        forked = sum(1 for s in sessions if s.get("forked_from"))
        rows.append(("Sessions", ", ".join(f"{p} ×{n}" for p, n in by.items())
                     + (f" ({forked} forked from your existing sessions)" if forked else "")))
    if rows:
        lines.append("")
        lines += [f"  {k:<9} {v}" for k, v in rows]

    changed = [s for s in task["steps"] if s["kind"] == "implement" and s["status"] == "done" and s.get("summary")]
    if changed and task["intent"] != "answer":
        lines += ["", "What changed"]
        latest = {}
        for s in changed:
            latest.setdefault(s["project"], s["summary"])  # first (main) implement step per project
        lines += [f"  - {p}: {short(text, 150)}" for p, text in latest.items()]

    open_q = [q for q in task["questions"] if not q.get("answer")]
    if open_q and questions:
        lines += ["", "Needs your decision"]
        for i, q in enumerate(open_q, 1):
            opts = f"  [{' / '.join(option_labels(q))}]" if q.get("options") else ""
            first, *rest = q["text"].splitlines()
            lines.append(f"  {i}. {first}{opts}")
            lines += [f"     {r.strip()}" if not r.startswith("  ") else f"   {r}" for r in rest]
        lines.append("  → just type your answer" if chat else f"  → foreman answer {task['id']} \"<your answer>\"")

    if task["remaining"]:
        lines += ["", "Remaining"] + [f"  - {short(r, 200)}" for r in task["remaining"][:6]]

    if task.get("notices"):
        lines += ["", "Notes"] + [f"  - {short(n, 200)}" for n in task["notices"][:5]]

    nxt = task.get("next")
    if nxt and not open_q and not nxt.get("resume"):  # an unfinished task's way forward is "continue", said above
        lines += ["", "Next step", f"  {nxt.get('title') or nxt['mode']}: {nxt.get('reason', '')}",
                  "  → say \"next\" to start it, or ask for something else" if chat else "  → foreman next"]

    if task.get("next_for_you") and not open_q:
        lines += ["", "Next for you"] + [f"  - {x}" for x in task["next_for_you"]]

    new_sugg = [s for s in (all_suggestions or []) if s["id"] in task.get("suggestions", [])]
    if new_sugg:
        lines += ["", "Worth considering (your call)"]
        for s in new_sugg:
            lines.append(f"  [{s['id']}] {s['text']}" + (f" — {short(s['why'], 140)}" if s.get("why") else ""))
        lines.append("  → say \"do s1\" or \"dismiss s1\"" .replace("s1", new_sugg[0]["id"]) if chat
                     else "  → foreman accept <id>  |  foreman dismiss <id>")

    lines += ["", f"Details: /show {task['id']}" if chat else f"Details: foreman show {task['id']}"]
    return "\n".join(lines)


def render_plan(task: Dict[str, Any], chat: bool = False) -> str:
    """Dry-run view: what Foreman would do for the request. Nothing has been executed or changed."""
    lines = [f"◌ #{task['id']} {task['title']} — plan only (dry run: no worker ran, nothing was changed)"]
    if task["status"] == "blocked":
        lines = [f"✘ #{task['id']} {task['title']} — planning failed (dry run)"]
    if task.get("answer") and task["intent"] == "answer":
        lines += ["", task["answer"].strip()]
    rows = []
    if task.get("mode"):
        how = "chosen by you" if task.get("mode_source") == "user" else \
            "recommended" + (f": {task['mode_reason']}" if task.get("mode_reason") else "")
        rows.append(("Mode", f"{task.get('mode_title') or task['mode']} ({how})"))
    if task["projects"]:
        rows.append(("Projects", _projects_line(task)))
    impact = task.get("impact") or {}
    if impact:
        rows.append(("Impact", f"{impact.get('risk', '?')} risk: {short(impact.get('summary', ''), 160)}"))
    if rows:
        lines.append("")
        lines += [f"  {k:<9} {v}" for k, v in rows]
    if task["steps"]:
        lines += ["", "Steps"]
        for s in task["steps"]:
            after = f" (after {', '.join(s['depends_on'])})" if s.get("depends_on") else ""
            lines.append(f"  {s['id']:<6} {s['project']} · {s['kind']}{after}: {short(s['instructions'], 130)}")
    if task["checks"]:
        lines += ["", "Checks Foreman would run"]
        lines += [f"  {c['project']}: {c['command']}" + ("" if c.get("allowed", True) else f"  (not allowed: {c.get('blocked_reason', '')})")
                  for c in task["checks"]]
    open_q = [q for q in task["questions"] if not q.get("answer")]
    if open_q:
        lines += ["", "Needs your decision before it can run"]
        lines += [f"  {i}. {q['text'].splitlines()[0]}" for i, q in enumerate(open_q, 1)]
    if task["remaining"]:
        lines += ["", "Problems"] + [f"  - {short(r, 200)}" for r in task["remaining"][:4]]
    if task.get("notices"):
        lines += ["", "Notes"] + [f"  - {short(n, 200)}" for n in task["notices"][:5]]
    if task["status"] == "planned" and task["steps"]:
        lines += ["", "  → say \"continue\" to run this plan" if chat else f"  → foreman resume {task['id']}   (runs this plan, no new planning)"]
    return "\n".join(lines)


def task_summary(task: Dict[str, Any]) -> Dict[str, Any]:
    """Compact machine-readable view of a task (for --json output)."""
    return {
        "id": task["id"], "status": task["status"], "title": task["title"], "request": task["request"],
        "intent": task.get("intent"), "mode": task.get("mode"), "feature": task.get("feature"),
        "cwd": task["cwd"], "updated_at": task["updated_at"], "dry_run": bool(task.get("dry_run")),
        "projects": {n: e.get("role") for n, e in task["projects"].items()},
        "steps": [{"id": s["id"], "project": s["project"], "kind": s["kind"], "status": s["status"],
                   "provider": s.get("provider"), "summary": s.get("summary", "")} for s in task["steps"]],
        "checks": [{"project": e["project"], "command": e["command"], "ok": e["ok"]}
                   for e in task["evidence"] if e["kind"] == "check"],
        "questions": [q["text"] for q in task["questions"] if not q.get("answer")],
        "remaining": task["remaining"], "next": task.get("next"), "answer": task.get("answer"),
        "next_for_you": task.get("next_for_you") or [],
    }


def render_status(task: Dict[str, Any], changed_since: Dict[str, List[str]],
                  others: Optional[List[Dict[str, Any]]] = None) -> str:
    lines = [f"{ICON.get(task['status'], '•')} #{task['id']} {task['title']} — {task['status']}  (updated {task['updated_at']})",
             f"  Request   {short(task['request'], 200)}"]
    if task["projects"]:
        lines.append(f"  Projects  {_projects_line(task)}")
    done = [s for s in task["steps"] if s["status"] == "done"]
    if task["steps"]:
        lines.append(f"  Steps     {len(done)}/{len(task['steps'])} done")
        for s in task["steps"]:
            mark = {"done": "✔", "pending": "·", "running": "…", "needs_input": "?", "paused": "‖"}.get(s["status"], "✘")
            if s.get("skipped_by_user"):
                mark = "–"
            lines.append(f"    {mark} {s['id']} {s['project']} {s['kind']}: "
                         f"{short(s.get('summary') or s['instructions'], 110)}")
    checks = {}
    for e in task["evidence"]:
        if e["kind"] == "check":
            checks[(e["project"], e["command"])] = e
    if checks:
        lines.append("  Checks    " + "  ".join(f"{'✔' if e['ok'] else '✘'} {e['project']}" for e in checks.values()))
    if task.get("answer") and task["intent"] == "answer":
        lines.append(f"  Answer    {short(task['answer'], 300)}")
    if task.get("waiting"):
        w = task["waiting"]
        lines.append(f"  Waiting   {w.get('reason', '')}; next try at {clock(w.get('until'))}"
                     + ("" if w.get("known") else f" (check {w.get('check')}/{w.get('of')})"))
    where = stopped_at(task) if task["status"] in ("interrupted", "paused", "blocked", "partial") else ""
    if where:
        lines.append(f"  Stopped   at {where}; `foreman resume {task['id']}` continues there")
    open_q = [q for q in task["questions"] if not q.get("answer")]
    for q in open_q:
        opts = f"  [{' / '.join(option_labels(q))}]" if q.get("options") else ""
        lines.append(f"  Question  {short(q['text'], 300)}{opts}")
    for r in task["remaining"][:5]:
        lines.append(f"  Remaining {short(r, 160)}")
    for proj, files in changed_since.items():
        lines.append(f"  ⚠ {proj} changed since this task last ran ({len(files)} files: {', '.join(files[:4])}"
                     f"{'…' if len(files) > 4 else ''}) — recorded results may be outdated; `foreman resume {task['id']}` re-verifies")
    if others:
        lines.append("  Also open: " + ", ".join(f"#{t['id']} {short(t['title'], 40)}" for t in others[:4]))
    return "\n".join(lines)
