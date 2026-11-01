// The app's state and its one connection to Foreman. The app is organised like a chat app: projects with their
// conversations in the sidebar, the open conversation in the middle, its tasks shown where they were started.
// task.json (tasks.get) is the truth; notifications only say when to read it again.

import { Rpc, RpcError, bridgeTransport, inTauri, tauriTransport } from "./rpc.js";
import { mergeTasks, newer, isLive } from "./tasks.js";
import { describe as describeEvent } from "./events.js";
import { applyTheme, savedTheme } from "./theme.js";
import { pickFolder } from "./shell.js";

const ACTIVITY_LINES = 6; // the latest activity lines a running task shows in the conversation

export const app = $state({
  connected: false,
  connecting: true,
  demo: false,
  failure: null, // {message, log} when Foreman couldn't start or stopped
  hello: null,
  projects: [],
  conversations: [], // [{id, project, title, updated_at, tasks, messages}]
  tasks: [], // TaskSummary of every task (sidebar status, "Needs you")
  suggestions: [],
  config: null,
  // what the middle shows: {kind: "new", cwd} | {kind: "conversation", project, id} | {kind: "task", id}
  open: { kind: "new", cwd: "" },
  thread: null, // {conversation, tasks: {id: TaskSummary}} of the open conversation
  pending: null, // the message being sent, shown at once: {text}
  draft: "", // text to put in the message box (a suggestion clicked); the box takes it and clears this
  details: {}, // task id -> TaskDetail (report_md, record)
  activity: {}, // task id -> the latest activity lines [{text, at}]
  events: {}, // task id -> all described events (the side panel's timeline)
  phases: {}, // task id -> {text, provider}
  panel: null, // {taskId, tab: "activity" | "output"} when the side panel is open
  settings: null, // the settings section shown in the settings dialog, null when closed
  notice: "",
  closing: false,
  theme: savedTheme(),
});

let rpc = null;
const cursors = {};
const timers = {};

export function call(method, params, timeoutMs) {
  if (!rpc) return Promise.reject(new RpcError("Not connected", "disconnected"));
  return rpc.call(method, params, timeoutMs);
}

let noticeTimer = null;
export function say(text) {
  app.notice = text;
  clearTimeout(noticeTimer);
  if (text) noticeTimer = setTimeout(() => (app.notice = ""), Math.min(12000, 4000 + text.length * 40));
}

export function setTheme(theme) {
  app.theme = theme;
  applyTheme(theme);
}

function later(name, fn, ms = 150) {
  clearTimeout(timers[name]);
  timers[name] = setTimeout(() => Promise.resolve(fn()).catch(() => {}), ms);
}

// ------------------------------------------------------------------ connection
export async function connect() {
  app.connecting = true;
  app.failure = null;
  try {
    if (!rpc) {
      let transport;
      if (inTauri()) transport = await tauriTransport();
      else if ((transport = bridgeTransport())) app.demo = false;
      else {
        app.demo = true;
        transport = (await import("./fake.js")).fakeTransport();
      }
      rpc = new Rpc(transport);
      rpc.on("event", onEvent);
      rpc.on("phase", (p) => (app.phases[p.task_id] = { text: p.text, provider: p.provider }));
      rpc.on("tasks_changed", (p) => changed(p.task_ids || []));
      rpc.on("resync", (p) => {
        cursors[p.task_id] = p.cursor;
        changed([p.task_id]);
      });
      rpc.on("disconnected", (info) => {
        app.connected = false;
        app.failure = { message: info.message || "Foreman stopped.", log: info.log || "" };
      });
    }
    app.hello = await rpc.connect();
    app.connected = true;
    await Promise.all([loadProjects(), loadTasks(), loadConversations(), loadConfig(), loadSuggestions()]);
    await call("events.subscribe", { tasks: "active", since: { ...cursors } });
    if (app.open.kind === "new" && !app.open.cwd) app.open = { kind: "new", cwd: lastFolder() };
    if (app.open.kind !== "new") await reloadThread();
  } catch (e) {
    app.connected = false;
    app.failure = { message: e.message || String(e), log: e.data?.log || "" };
  } finally {
    app.connecting = false;
  }
}

// ------------------------------------------------------------------ loading
export async function loadProjects() {
  app.projects = (await call("projects.list", {})).projects;
}
export async function loadTasks() {
  const res = await call("tasks.list", { limit: 300 });
  app.tasks = mergeTasks([], res.tasks);
  for (const id of Object.keys(app.phases)) {
    const t = app.tasks.find((x) => x.id === id);
    if (!t || !isLive(t)) delete app.phases[id];
  }
}
export async function loadConversations() {
  app.conversations = (await call("conversations.list", {})).conversations;
}
export async function loadConfig() {
  app.config = await call("config.get", {});
}
export async function loadSuggestions() {
  app.suggestions = (await call("suggestions.list", { status: "open" })).suggestions;
}

/** The folder a conversation's project lives in (a conversation outside any project is keyed by its folder). */
export function folderOf(project) {
  return app.projects.find((p) => p.name === project)?.path || project;
}

export function lastFolder() {
  try {
    const saved = localStorage.getItem("foreman.cwd");
    if (saved) return saved;
  } catch {}
  return app.projects[0]?.path || "";
}

/** Where a task was started: its conversation, if any. */
export function conversationOf(taskId) {
  return app.conversations.find((c) => c.tasks.includes(String(taskId))) || null;
}

const RELOAD_ON = new Set(["run_started", "status", "question_opened", "finished", "closed", "resumed", "answered",
  "dry_run", "plan", "step_done", "step_start", "check", "created"]);

function onEvent({ task_id, cursor, event }) {
  cursors[task_id] = cursor;
  const text = describeEvent(event);
  if (text) {
    const line = { text, at: event.ts || 0, event: event.event };
    app.events[task_id] = [...(app.events[task_id] || []), line].slice(-2000);
    app.activity[task_id] = [...(app.activity[task_id] || []), line].slice(-ACTIVITY_LINES);
  }
  if (RELOAD_ON.has(event.event)) changed([task_id]);
}

function changed(ids) {
  later("tasks", loadTasks);
  later("conversations", loadConversations, 300);
  const shown = threadTaskIds();
  if (ids.some((id) => shown.includes(id))) later("thread", reloadThread, 200);
}

function threadTaskIds() {
  if (app.open.kind === "task") return [app.open.id];
  return Object.keys(app.thread?.tasks || {});
}

// ------------------------------------------------------------------ what the middle shows
export async function openConversation(project, id) {
  app.open = { kind: "conversation", project, id };
  app.thread = null;
  app.pending = null;
  await reloadThread();
}

export async function openTask(id) {
  const conv = conversationOf(id);
  if (conv) return openConversation(conv.project, conv.id);
  app.open = { kind: "task", id: String(id) };
  app.thread = null;
  await reloadThread();
}

export function newConversation(cwd) {
  app.open = { kind: "new", cwd: cwd || (app.open.kind === "conversation" ? folderOf(app.open.project) : lastFolder()) };
  app.thread = null;
  app.pending = null;
  app.panel = null;
}

export async function reloadThread() {
  const open = app.open;
  try {
    let thread;
    if (open.kind === "conversation") {
      thread = await call("conversations.get", { project: open.project, conversation_id: open.id });
    } else if (open.kind === "task") {
      const detail = await call("tasks.get", { task_id: open.id });
      thread = { conversation: null, tasks: { [open.id]: detail.summary } };
    } else return;
    if (app.open !== open) return;
    for (const [id, t] of Object.entries(thread.tasks)) thread.tasks[id] = newer(app.thread?.tasks?.[id], t);
    app.thread = thread;
    await Promise.all(Object.keys(thread.tasks).map(loadDetail));
  } catch (e) {
    say(e.message);
  }
}

async function loadDetail(id) {
  const detail = await call("tasks.get", { task_id: id });
  app.details[id] = detail;
  if (!app.events[id]) {
    const page = await call("tasks.events", { task_id: id, cursor: 0, limit: 2000 });
    const lines = page.events.map((e) => ({ text: describeEvent(e), at: e.ts || 0, event: e.event })).filter((l) => l.text);
    app.events[id] = lines;
    app.activity[id] = lines.slice(-ACTIVITY_LINES);
    cursors[id] = page.cursor;
    if (isLive(detail.summary)) await call("events.subscribe", { tasks: "active", since: { ...cursors } });
  }
}

// ------------------------------------------------------------------ actions
/** Send a message in the open conversation (or start one). */
export async function send(text, { mode = null, dryRun = false, key } = {}) {
  const open = app.open;
  const cwd = open.kind === "conversation" ? folderOf(open.project) : open.kind === "new" ? open.cwd
    : folderOf(app.thread?.tasks?.[open.id]?.project || ""); // a task outside conversations: a new one in its project
  if (!cwd) return null;
  app.pending = { text };
  try {
    const res = await call("conversations.send", {
      cwd, text, conversation_id: open.kind === "conversation" ? open.id : null, mode, dry_run: dryRun, idempotency_key: key,
    });
    try {
      localStorage.setItem("foreman.cwd", cwd);
    } catch {}
    await loadConversations();
    if (res.task_id) later("tasks", loadTasks, 0);
    if (open.kind !== "conversation" || open.id !== res.conversation_id) await openConversation(res.project, res.conversation_id);
    else await reloadThread();
    return res;
  } catch (e) {
    say(e.kind === "running" ? "The task is running; this can be done after its current steps." : e.message);
    return null;
  } finally {
    app.pending = null;
  }
}

/** A task action (stop, continue, close, answer): reports the outcome in one line; null when refused. */
export async function act(method, params, done) {
  try {
    const res = await call(method, params);
    if (res?.message) say(res.message);
    else if (res?.reply) say(res.reply);
    else if (typeof done === "string") say(done);
    later("tasks", loadTasks, 0);
    later("thread", reloadThread, 0);
    return res ?? {};
  } catch (e) {
    say(e.kind === "running" ? "The task is running; this can be done after its current steps."
      : e.kind === "cancelled" ? "Not sent." : e.message);
    if (e.kind === "stale") later("thread", reloadThread, 0);
    return null;
  }
}

export async function acceptSuggestion(s) {
  const open = app.open;
  const cwd = open.kind === "conversation" ? folderOf(open.project) : folderOf(s.project) || lastFolder();
  const res = await act("suggestions.accept", { id: s.id, cwd, conversation_id: open.kind === "conversation" ? open.id : null });
  await Promise.all([loadSuggestions(), loadConversations()]);
  return res;
}

export async function dismissSuggestion(s) {
  await act("suggestions.dismiss", { id: s.id }, "Dismissed; it won't be suggested again.");
  await loadSuggestions();
}

/** Listed in the sidebar: a project the user opened (in any front end), or one worked in before projects were
 * marked opened (it has conversations or tasks). A hidden one (opened === false) is not listed until opened again. */
export function isListed(p) {
  if (typeof p.opened === "number") return true;
  return p.opened == null && (app.conversations.some((c) => c.project === p.name) || app.tasks.some((t) => t.project === p.name));
}

/** New conversation: the native folder picker; the folder becomes an opened project (projects.open). */
export async function openProject(start) {
  const path = await pickFolder(start || lastFolder());
  if (!path) return null;
  try {
    const p = await call("projects.open", { path });
    await loadProjects();
    newConversation(p.path);
    return p;
  } catch (e) {
    say(e.message);
    return null;
  }
}

export async function hideProject(name) {
  try {
    await call("projects.hide", { name });
    await loadProjects();
    if (app.open.kind === "conversation" && app.open.project === name) newConversation(lastFolder());
    say(`${name} is off the list; open its folder again to bring it back.`);
  } catch (e) {
    say(e.message);
  }
}

export function runningTasks() {
  return app.tasks.filter(isLive);
}
