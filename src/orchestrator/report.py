"""Concise reports, generated deterministically from the task record (never from AI free text alone)."""

import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from orchestrator.util import short

ICON = {"done": "✔", "answered": "✔", "partial": "◐", "needs_input": "?", "blocked": "✘", "failed": "✘",
        "running": "…", "planning": "…", "queued": "…", "interrupted": "!"}


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
    if st == "interrupted":
        lines += ["", "Stopped by you. " + ("Say \"continue\" to pick it up again." if chat
                                           else f"`foreman resume {task['id']}` picks it up again.")]

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
            opts = f"  [{' / '.join(q['options'])}]" if q.get("options") else ""
            first, *rest = q["text"].splitlines()
            lines.append(f"  {i}. {first}{opts}")
            lines += [f"     {r.strip()}" if not r.startswith("  ") else f"   {r}" for r in rest]
        lines.append("  → just type your answer" if chat else f"  → foreman answer {task['id']} \"<your answer>\"")

    if task["remaining"]:
        lines += ["", "Remaining"] + [f"  - {short(r, 200)}" for r in task["remaining"][:6]]

    if task.get("notices"):
        lines += ["", "Notes"] + [f"  - {short(n, 200)}" for n in task["notices"][:5]]

    nxt = task.get("next")
    if nxt and not open_q:
        lines += ["", "Next step", f"  {nxt.get('title') or nxt['mode']}: {nxt.get('reason', '')}",
                  "  → say \"next\" to start it, or ask for something else" if chat else "  → foreman next"]

    new_sugg = [s for s in (all_suggestions or []) if s["id"] in task.get("suggestions", [])]
    if new_sugg:
        lines += ["", "Worth considering (your call)"]
        for s in new_sugg:
            lines.append(f"  [{s['id']}] {s['text']}" + (f" — {short(s['why'], 140)}" if s.get("why") else ""))
        lines.append("  → say \"do s1\" or \"dismiss s1\"" .replace("s1", new_sugg[0]["id"]) if chat
                     else "  → foreman accept <id>  |  foreman dismiss <id>")

    lines += ["", f"Details: /show {task['id']}" if chat else f"Details: foreman show {task['id']}"]
    return "\n".join(lines)


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
            mark = {"done": "✔", "pending": "·", "running": "…"}.get(s["status"], "✘")
            lines.append(f"    {mark} {s['project']} {s['kind']}: {short(s.get('summary') or s['instructions'], 110)}")
    checks = {}
    for e in task["evidence"]:
        if e["kind"] == "check":
            checks[(e["project"], e["command"])] = e
    if checks:
        lines.append("  Checks    " + "  ".join(f"{'✔' if e['ok'] else '✘'} {e['project']}" for e in checks.values()))
    if task.get("answer") and task["intent"] == "answer":
        lines.append(f"  Answer    {short(task['answer'], 300)}")
    open_q = [q for q in task["questions"] if not q.get("answer")]
    for q in open_q:
        lines.append(f"  Waiting   {q['text']}")
    for r in task["remaining"][:5]:
        lines.append(f"  Remaining {short(r, 160)}")
    for proj, files in changed_since.items():
        lines.append(f"  ⚠ {proj} changed since this task last ran ({len(files)} files: {', '.join(files[:4])}"
                     f"{'…' if len(files) > 4 else ''}) — recorded results may be outdated; `foreman resume {task['id']}` re-verifies")
    if others:
        lines.append("  Also open: " + ", ".join(f"#{t['id']} {short(t['title'], 40)}" for t in others[:4]))
    return "\n".join(lines)
