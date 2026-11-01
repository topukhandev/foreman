// One line per event for the activity view (the chat's progress lines, without terminal styling). Quiet events
// return null. Text from AI is shown as text only.

const OUTCOME = { limit: "usage limit", transient: "connection lost", timeout: "out of time", output: "unreadable output", login: "not logged in" };

export function describe(e) {
  switch (e.event) {
    case "created": return `Created${e.actor ? ` (${e.actor})` : ""}`;
    case "mode": return `Mode: ${e.title || e.mode}${e.source === "user" ? " (your choice)" : ""}`;
    case "thinking": return `${{ plan: "Planning", replan: "Re-planning", review: "Reviewing", document: "Writing" }[e.purpose] || "Thinking"}${e.provider ? ` · ${e.provider}` : ""}`;
    case "plan": {
      const steps = e.plan?.steps || [];
      return steps.length ? `Plan: ${steps.map((s) => `${s.kind} ${s.project}`).join("; ")}` : "Answered directly, no workers needed";
    }
    case "impact": return `Impact: ${e.risk || "?"} risk${(e.consumers || []).length ? `, consumers ${e.consumers.join(", ")}` : ""}`;
    case "step_start": return `${{ investigate: "Investigate", verify: "Verify" }[e.step_kind] || "Implement"} ${e.project} · ${e.provider}${e.why ? ` · ${e.why}` : ""}`;
    case "step_done": return `${e.status === "done" ? "✓" : "!"} ${e.project || ""} ${e.summary || e.status}`.trim();
    case "step_failed": return `${e.provider} ${OUTCOME[e.cause] ? `stopped: ${OUTCOME[e.cause]}` : "failed"}${e.error ? `: ${e.error}` : ""}`;
    case "step_retry": return `${e.provider} lost its connection; resuming its session in ${e.wait}s`;
    case "step_timeout": return `${e.provider} ran out of time; continuing its session`;
    case "waiting": return `Waiting: ${e.reason || ""}`;
    case "check": return `${e.ok ? "✓" : "✗"} Check ${e.project}: ${e.command}`;
    case "review": return `Review: ${e.review?.verdict || "?"}${e.review?.summary ? ` — ${e.review.summary}` : ""}`;
    case "question_opened": return `Question for you${e.question_kind ? ` (${e.question_kind})` : ""}`;
    case "answered": return `Answered${e.actor ? ` (${e.actor})` : ""}`;
    case "resumed": return `Resumed${e.actor ? ` (${e.actor})` : ""}`;
    case "stop_requested": return `Stop requested${e.actor ? ` (${e.actor})` : ""}`;
    case "closed": return `${e.how || "Closed"}${e.note ? `: ${e.note}` : ""}`;
    case "status": return `Status: ${e.status}${e.reason ? ` (${e.reason})` : ""}`;
    case "notice": return e.text || e.message || null;
    case "document": return `Saved ${e.project}/${e.path}`;
    case "replan": return `Expanding the plan: ${(e.discoveries || []).join("; ")}`;
    case "dry_run": return `Planned only (${e.status})`;
    case "finished": return `Finished: ${e.status || ""}`;
    case "activity": return `${e.where ? `[${e.where}] ` : ""}${e.text || ""}`;
    case "output": return null;
    default: return null;
  }
}

export function clockTime(e) {
  const t = e.ts ? new Date(e.ts * 1000) : e.at ? new Date(String(e.at).replace(" ", "T")) : null;
  return t && !Number.isNaN(t.getTime()) ? t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "";
}
