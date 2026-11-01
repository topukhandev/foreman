// Pure helpers over the API's data (TaskSummary, Question): what needs the user, what a status means, which snapshot
// is newer. No I/O here, so it is tested directly (tests/tasks.test.js).

export const LIVE = new Set(["queued", "planning", "running"]);
export const FINAL = new Set(["done", "failed", "answered", "cancelled"]);
// Waiting for the user: a question, a decision, or a run that stopped short.
export const ATTENTION = new Set(["needs_input", "blocked", "partial", "interrupted", "paused", "failed"]);

export const STATUS_TEXT = {
  queued: "Starting", planning: "Planning", running: "Running", needs_input: "Needs your answer",
  blocked: "Blocked", partial: "Partly done", interrupted: "Stopped", paused: "Paused (usage limits)",
  planned: "Planned (dry run)", done: "Done", answered: "Answered", failed: "Failed", cancelled: "Cancelled",
};

// How far a result is verified (engine._verification). Only tool_verified is shown as green.
export const LEVEL_TEXT = {
  tool_verified: "Verified by tests (Foreman ran them)",
  reviewed: "Approved by QA (AI), not verified by tests",
  unverified: "Not verified",
  failed: "Verification failed",
};

export const CRITERION_BASIS = { claimed: "worker's word", evidenced: "worker, with evidence", reviewed: "checked by QA" };

/** The badge colour for a task's status: info while it runs, ok when done, bad when it failed, warn when it waits. */
export function tone(task) {
  if (isLive(task)) return "info";
  if (task.status === "done" || task.status === "answered") return "ok";
  if (task.status === "failed" || task.status === "blocked") return "bad";
  return task.status === "cancelled" ? "" : "warn";
}

export function isLive(task) {
  return Boolean(task && (task.running || LIVE.has(task.status)));
}

/** Tasks for the home view, in groups: needs you, running, planned (dry runs waiting for "run it"). */
export function inbox(tasks) {
  const needs = [], running = [], planned = [];
  for (const t of tasks) {
    if (isLive(t)) running.push(t);
    else if ((t.questions || []).length || ATTENTION.has(t.status)) needs.push(t);
    else if (t.status === "planned") planned.push(t);
  }
  return { needs, running, planned };
}

/** The snapshot to keep: the one with the higher rev (task.json is the truth; events are hints). */
export function newer(current, incoming) {
  if (!current) return incoming;
  if (!incoming) return current;
  return (incoming.rev ?? 0) >= (current.rev ?? 0) ? incoming : current;
}

/** Replace tasks in a list by id, keeping the newer snapshot of each; new ones go first. */
export function mergeTasks(list, incoming) {
  const byId = new Map(list.map((t) => [t.id, t]));
  for (const t of incoming) byId.set(t.id, newer(byId.get(t.id), t));
  return [...byId.values()].sort((a, b) => Number(b.id) - Number(a.id));
}

/** Questions that need a native confirmation in the shell before they are answered (high impact, new dependency). */
export const CONFIRMED_KINDS = new Set(["approval", "dependency"]);

/** What can be done with a task now. */
export function actions(task) {
  const live = isLive(task);
  const final = FINAL.has(task.status);
  return {
    stop: live,
    resume: !live && !final && task.status !== "needs_input",
    run: !live && task.status === "planned",
    close: !live && !final,
    answer: !live && (task.questions || []).length > 0,
  };
}

/** Options for a question as [{label, description, recommended}]. */
export function options(question) {
  const labels = question.options || [];
  const desc = question.descriptions || [];
  return labels.map((label, i) => ({ label: String(label), description: desc[i] || "", recommended: label === question.default }));
}

export function stepCounts(task) {
  const steps = task.steps || [];
  return { done: steps.filter((s) => s.status === "done").length, total: steps.length };
}

/** Models matching every word of a search (each word a case-insensitive regex, tried on the id and the name), like
 * the terminal menu. bad: a word isn't a valid regex (then nothing is filtered). */
export function searchModels(models, query) {
  const words = String(query || "").trim().split(/\s+/).filter(Boolean);
  if (!words.length) return { models, bad: false };
  let res;
  try {
    res = words.map((w) => new RegExp(w, "i"));
  } catch {
    return { models, bad: true };
  }
  return { models: models.filter((m) => res.every((r) => r.test(`${m.id} ${m.name || ""}`))), bad: false };
}

/** "just now", "5 min ago", "3 h ago", "2 days ago" from Foreman's local "YYYY-MM-DD HH:MM:SS" or epoch seconds. */
export function ago(stamp, now = Date.now()) {
  const t = typeof stamp === "number" ? stamp * 1000 : Date.parse(String(stamp || "").replace(" ", "T"));
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, (now - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  const d = Math.floor(s / 86400);
  return d === 1 ? "yesterday" : d < 30 ? `${d} days ago` : new Date(t).toISOString().slice(0, 10);
}

/** Why a task needs the user, in a few words, or "" when it doesn't. */
export function needsYou(task) {
  if (isLive(task)) return "";
  const q = (task.questions || []).length;
  if (q) return `${q} question${q > 1 ? "s" : ""}`;
  return { blocked: "Blocked", partial: "Partly done", interrupted: "Stopped", paused: "Paused", failed: "Failed" }[task.status] || "";
}
