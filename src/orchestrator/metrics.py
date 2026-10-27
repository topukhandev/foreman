"""Execution metrics (deterministic): what a task cost and how its runs went, from the task record and its event log,
and totals across tasks for `foreman stats`. Only what the provider CLIs actually report is counted (time always;
tokens and cost where the CLI prints them); a missing value is left out, never estimated."""

from datetime import datetime
from typing import Any, Dict, List, Optional

TOKEN_KEYS = ("input", "output", "reasoning", "cache_read", "cache_write")


def usage_fields(result: Any) -> Dict[str, Any]:
    """Duration, cost and tokens of one provider run, for the attempt journal or an event."""
    out: Dict[str, Any] = {}
    if getattr(result, "duration", 0):
        out["duration"] = round(float(result.duration), 1)
    if getattr(result, "cost_usd", None) is not None:
        out["cost_usd"] = round(float(result.cost_usd), 4)
    if getattr(result, "tokens", None):
        out["tokens"] = dict(result.tokens)
    return out


def _secs(a: str, b: str) -> float:
    try:
        return max(0.0, (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds())
    except (TypeError, ValueError):
        return 0.0


def task_metrics(task: Dict[str, Any], events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Totals over every run of a task so far."""
    runs: List[Dict[str, Any]] = [a for s in task.get("steps") or [] for a in s.get("history") or []]
    runs += [e for e in events if e.get("event") == "thinking_done"]
    tokens = {k: 0 for k in TOKEN_KEYS}
    cost, priced, busy = 0.0, 0, 0.0
    for r in runs:
        busy += float(r.get("duration") or 0)
        if r.get("cost_usd") is not None:
            cost += float(r["cost_usd"])
            priced += 1
        for k, v in (r.get("tokens") or {}).items():
            if k in tokens:
                tokens[k] += int(v or 0)
    handoffs = 0
    for s in task.get("steps") or []:
        names = [a.get("provider") for a in s.get("history") or []]
        handoffs += sum(1 for a, b in zip(names, names[1:]) if a != b)
    active, started = 0.0, None
    for e in events:  # time a run was actually going: from each run's start to its end
        if e.get("event") == "run_started":
            started = e.get("at")
        elif e.get("event") == "finished" and started:
            active += _secs(started, e.get("at", ""))
            started = None
    checks = [e for e in task.get("evidence") or [] if e.get("kind") == "check"]
    asked: Dict[str, int] = {}
    for q in task.get("questions") or []:
        asked[q.get("kind") or "other"] = asked.get(q.get("kind") or "other", 0) + 1
    out = {
        "calls": len(runs),
        "worker_calls": sum(1 for r in runs if "provider" in r and "event" not in r),
        "failed_calls": sum(1 for r in runs if r.get("outcome") not in (None, "done") or r.get("ok") is False),
        "retries": sum(1 for e in events if e.get("event") in ("step_retry", "step_timeout")),
        "handoffs": handoffs,
        "fix_rounds": int(task.get("fix_rounds") or 0),
        "replans": int(task.get("replans") or 0),
        "checks": len(checks),
        "checks_failed": sum(1 for e in checks if not e.get("ok")),
        "questions": asked,
        "busy_sec": round(busy, 1),
        "active_sec": round(active, 1),
        "tokens": {k: v for k, v in tokens.items() if v},
    }
    if priced:
        out["cost_usd"] = round(cost, 4)
        out["priced_calls"] = priced
    return out


def stats(tasks: List[Dict[str, Any]], since: Optional[str] = None) -> Dict[str, Any]:
    """Totals across tasks (optionally only those created at or after `since`, an ISO date)."""
    tasks = [t for t in tasks if not since or str(t.get("created_at", "")) >= since]
    by_status: Dict[str, int] = {}
    by_level: Dict[str, int] = {}
    totals = {"calls": 0, "failed_calls": 0, "retries": 0, "handoffs": 0, "fix_rounds": 0, "checks": 0,
              "checks_failed": 0, "busy_sec": 0.0, "active_sec": 0.0}
    cost, priced, measured, asked = 0.0, 0, 0, 0
    for t in tasks:
        by_status[t["status"]] = by_status.get(t["status"], 0) + 1
        level = (t.get("verification") or {}).get("level")
        if level:
            by_level[level] = by_level.get(level, 0) + 1
        m = t.get("metrics")
        if not m:
            continue
        measured += 1
        for k in totals:
            totals[k] += m.get(k) or 0
        if m.get("cost_usd") is not None:
            cost += m["cost_usd"]
            priced += 1
        asked += 1 if m.get("questions") else 0
    finished = sum(n for s, n in by_status.items() if s in ("done", "answered", "partial", "failed", "blocked", "cancelled"))
    achieved = by_status.get("done", 0) + by_status.get("answered", 0)
    return {"tasks": len(tasks), "measured": measured, "by_status": by_status, "verification": by_level,
            "completion_rate": round(achieved / finished, 2) if finished else None,
            "tasks_with_questions": asked, "totals": {k: round(v, 1) for k, v in totals.items()},
            "per_task": {k: round(v / measured, 1) for k, v in totals.items()} if measured else {},
            **({"cost_usd": round(cost, 2), "priced_tasks": priced} if priced else {})}


def render_stats(s: Dict[str, Any]) -> str:
    if not s["tasks"]:
        return "No tasks recorded yet."
    lines = [f"{s['tasks']} tasks ({s['measured']} with metrics)"]
    lines.append("  Status      " + ", ".join(f"{k} {v}" for k, v in sorted(s["by_status"].items(), key=lambda kv: -kv[1])))
    if s["completion_rate"] is not None:
        lines.append(f"  Achieved    {int(s['completion_rate'] * 100)}% of finished tasks (done or answered)")
    if s["verification"]:
        lines.append("  Verified    " + ", ".join(f"{k} {v}" for k, v in s["verification"].items()))
    if s["measured"]:
        p, t = s["per_task"], s["totals"]
        lines.append(f"  Per task    {p['calls']} model calls, {p['retries']} retries, {p['handoffs']} handoffs, "
                     f"{p['fix_rounds']} fix rounds, {int(p['active_sec'] // 60)} min running")
        lines.append(f"  Checks      {int(t['checks'])} run, {int(t['checks_failed'])} failed")
        lines.append(f"  Questions   {s['tasks_with_questions']} of {s['measured']} tasks needed your input")
    if "cost_usd" in s:
        lines.append(f"  Cost        ${s['cost_usd']} reported by providers ({s['priced_tasks']} tasks; others don't report cost)")
    return "\n".join(lines)
