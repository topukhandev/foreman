// Demo data for working on the UI in a plain browser (`npm run dev`), where there is no Tauri shell and no sidecar.
// The app shows a "Demo data" banner whenever this is in use. Nothing here reaches Foreman.

const now = "2026-11-01 10:00:00";

const TASKS = [
  {
    id: "3", project: "orders-api", title: "Refund endpoint", status: "needs_input", rev: 7, running: false, mode: "development",
    intent: "change", cwd: "/home/demo/orders-api", created_at: now, updated_at: now, dry_run: false,
    request: "Add a refund endpoint to orders", projects: { "orders-api": "modify", "admin-ui": "context" },
    steps: [{ id: "1", project: "orders-api", kind: "implement", status: "pending", provider: null, summary: "" }],
    checks: [], remaining: [], next: null, answer: null, next_for_you: [], verification: null, acceptance: [],
    questions: [{
      id: "q1", kind: "approval", header: "Approval", text: "This changes how refunds are paid out. Go ahead?",
      options: ["Yes, go ahead", "No"], descriptions: ["Run the plan as shown", "Cancel the task"],
      default: "Yes, go ahead", plan_fp: "demo", from_step: null, project: "orders-api",
    }],
  },
  {
    id: "2", project: "admin-ui", title: "Paginate the users table", status: "running", rev: 4, running: true, mode: "development",
    intent: "change", cwd: "/home/demo/admin-ui", created_at: now, updated_at: now, dry_run: false,
    request: "Paginate the admin users table", projects: { "admin-ui": "modify" },
    steps: [{ id: "1", project: "admin-ui", kind: "implement", status: "running", provider: "claude", summary: "" }],
    checks: [], questions: [], remaining: [], next: null, answer: null, next_for_you: [], verification: null,
    acceptance: [],
  },
  {
    id: "1", project: "admin-ui", title: "Footer", status: "done", rev: 9, running: false, mode: "development", intent: "change",
    cwd: "/home/demo/admin-ui", created_at: now, updated_at: now, dry_run: false, request: "Add a footer",
    projects: { "admin-ui": "modify" },
    steps: [{ id: "1", project: "admin-ui", kind: "implement", status: "done", provider: "claude",
      summary: "Added the footer component and its test." }],
    checks: [{ project: "admin-ui", command: "npm test", ok: true }], questions: [], remaining: [], next: "qa",
    answer: null, next_for_you: ["Look at the footer on a narrow screen."],
    verification: { level: "tool_verified", gaps: [] },
    acceptance: [{ id: "AC1", text: "A footer shows on every page", status: "pass", level: "evidenced", evidence: "" }],
  },
];

const REPORT = "## Footer\n\nAdded the footer. **Checks:** `npm test` passed.\n\n"
  + "A link: [Svelte](https://svelte.dev) and one that is dropped: [x](javascript:alert(1)).\n";

const yes = (key, label, value, applies = "all") => ({ key, label, kind: "toggle", applies, value });
const choice = (key, label, about, value, options) => ({ key, label, kind: "choice", applies: "all", about, value,
  options: options.map(([v, l, d, available = true]) => ({ value: v, label: l, description: d, available })) });
const SETTINGS = [
  yes("display", "Show workers in terminal windows", false),
  yes("auto_switch", "Switch provider automatically at a limit", true),
  choice("planner", "Planner (plans and reviews)", "The planner scopes requests.", "claude", [["claude", "claude", "ready"], ["opencode", "opencode", "not installed", false]]),
  choice("first_worker", "First worker", "The first ready one is used.", "claude", [["claude", "claude", "ready"], ["opencode", "opencode", "not installed", false]]),
  choice("output_style", "Output style (how answers are written)", "How answers are written.", "default", [["default", "Default", "Clear answers in short Markdown"], ["concise", "Concise", "As short as possible"]]),
  yes("prompt_suggestions", "Suggest your next message (Tab takes it)", true, "chat"),
  yes("status_line", "Status line under the input", true, "chat"),
  yes("notify", "Desktop notification when a task finishes", true),
  yes("reuse_sessions", "Start from your recent sessions", true),
  yes("copy_on_select", "Copy selected text in the chat", true, "chat"),
  choice("on_close", "Desktop app closed while tasks run", "Tasks run in their own processes.", "ask", [["ask", "Ask", "keep them running or stop them"], ["keep", "Keep running", "you get the usual notification"], ["stop", "Stop them", "continue them later"]]),
];
const PROVIDERS = {
  claude: { enabled: true, available: true, info: "demo", model: "demo-sonnet", effort: "high", levels: ["low", "medium", "high", "max"], light: { model: "demo-sonnet", effort: "medium" } },
  opencode: { enabled: true, available: false, info: "not installed", model: "default", effort: null, levels: [], light: { model: "default", effort: null } },
};
const MODELS = [
  { id: "demo-sonnet", name: "Demo Sonnet", levels: ["low", "medium", "high", "max"], group: "" },
  { id: "demo-opus", name: "Demo Opus", levels: ["low", "medium", "high", "xhigh", "max"], group: "" },
  { id: "demo-haiku", name: "Demo Haiku", levels: [], group: "" },
];

const NOW = Date.now() / 1000;
const CONVERSATIONS = [
  { id: "c-refunds", project: "orders-api", title: "Refund endpoint", started_at: NOW - 3600, updated_at: NOW - 600,
    entries: [{ user: "Add a refund endpoint to orders", output: "#3 Refund endpoint — started", at: NOW - 3600, task: "3" }] },
  { id: "c-users", project: "admin-ui", title: "Paginate the users table", started_at: NOW - 900, updated_at: NOW - 60,
    entries: [{ user: "How are users listed today?", output: "The admin users table loads every user at once (src/users.js).", at: NOW - 900 },
      { user: "Paginate the admin users table", output: "#2 Paginate the users table — started", at: NOW - 800, task: "2" }] },
  { id: "c-footer", project: "admin-ui", title: "Footer", started_at: NOW - 90000, updated_at: NOW - 86000,
    entries: [{ user: "Add a footer", output: "#1 Footer — started", at: NOW - 90000, task: "1" }] },
];

function result(method, params) {
  switch (method) {
    case "system.hello":
      return { api_version: "1.3", core_version: "demo", home: "~/.foreman", platform: "demo", actor: "desktop",
        providers: [{ name: "claude", available: true, info: "demo" }, { name: "opencode", available: false, info: "not installed" }],
        schemas: { task: 1, registry: 1 } };
    case "tasks.list":
      return { tasks: TASKS, unreadable: [] };
    case "tasks.get": {
      const t = TASKS.find((x) => x.id === params.task_id);
      if (!t) throw { message: `No task #${params.task_id}.`, kind: "not_found" };
      return { summary: t, record: { id: t.id, request: t.request, impact: { risk: "high" } }, report_md: t.id === "1" ? REPORT : "" };
    }
    case "tasks.events":
      return { events: [{ event: "created", at: now, ts: 0, cursor: 10 }], cursor: 10, resync: false };
    case "tasks.outputs":
      return { outputs: [] };
    case "projects.list":
      return { projects: [
        { name: "admin-ui", path: "/home/demo/admin-ui", stack: ["node"], test: "npm test", source: "scan", opened: NOW - 60,
          relations: [{ type: "calls", other: "orders-api", direction: "out", evidence: "src/api.js", source: "discovered", confidence: "high" }] },
        { name: "orders-api", path: "/home/demo/orders-api", stack: ["node"], test: "npm test", source: "scan", opened: NOW - 600, relations: [] },
        { name: "billing", path: "/home/demo/billing", stack: ["go"], test: "go test ./...", source: "scan", opened: null, relations: [] },
      ] };
    case "projects.open":
      return { name: params.path.split("/").filter(Boolean).pop(), path: params.path, stack: [], test: null, source: "declared", opened: NOW, relations: [] };
    case "projects.hide":
      return { name: params.name, opened: false };
    case "sessions.list":
      return { sessions: [{ provider: "claude", session_id: "demo-1", updated_at: NOW, title: "Footer work" }] };
    case "system.doctor":
      return { providers: { claude: { available: true, info: "demo", runs_today: 3, max_runs_per_day: 100 } }, projects: 3, relations: 1, mcp: { claude: "registered" } };
    case "system.stats":
      return { stats: {}, text: "3 tasks (demo)" };
    case "suggestions.list":
      return { suggestions: [{ id: "s1", task: "1", status: "open", project: "admin-ui", text: "Add a test for the empty footer", why: "found while adding it" }] };
    case "config.get":
      return { display: "headless", auto_switch: true, notify: true, on_close: SETTINGS.find((x) => x.key === "on_close").value,
        planner: "claude", config_file: "~/.foreman/config.yaml", settings: SETTINGS, providers: PROVIDERS };
    case "config.set": {
      const item = SETTINGS.find((x) => x.key === params.key);
      if (!item) throw { message: `No setting '${params.key}'.`, kind: "invalid" };
      item.value = params.value;
      return { setting: item };
    }
    case "models.list":
      return { provider: params.provider, available: true, info: "demo", current: PROVIDERS[params.provider],
        models: MODELS, typed_levels: ["low", "medium", "high", "max"] };
    case "models.set": {
      const levels = MODELS.find((m) => m.id === params.model)?.levels || [];
      Object.assign(PROVIDERS[params.provider], { model: params.model, effort: levels.length ? params.effort || "high" : null });
      return { provider: params.provider, model: params.model, effort: PROVIDERS[params.provider].effort };
    }
    case "conversations.list":
      return { conversations: CONVERSATIONS.map(({ entries, ...c }) => ({ ...c, messages: entries.length,
        tasks: entries.map((e) => e.task).filter(Boolean) })) };
    case "conversations.get": {
      const c = CONVERSATIONS.find((x) => x.id === params.conversation_id);
      if (!c) throw { message: "No conversation.", kind: "not_found" };
      const tasks = Object.fromEntries(c.entries.filter((e) => e.task).map((e) => [e.task, TASKS.find((t) => t.id === e.task)]));
      return { conversation: { ...c, focus: c.entries.filter((e) => e.task).pop()?.task || null }, tasks };
    }
    case "conversations.send": {
      let c = CONVERSATIONS.find((x) => x.id === params.conversation_id);
      if (!c) {
        c = { id: `demo-${Date.now()}`, project: "admin-ui", title: params.text.slice(0, 60), started_at: Date.now() / 1000,
          updated_at: Date.now() / 1000, entries: [] };
        CONVERSATIONS.unshift(c);
      }
      const reply = "Demo data: nothing runs here. Start the app with `npm run tauri dev` to use Foreman.";
      c.entries.push({ user: params.text, output: reply, at: Date.now() / 1000 });
      c.updated_at = Date.now() / 1000;
      return { conversation_id: c.id, project: c.project, reply };
    }
    case "conversations.rename": {
      const c = CONVERSATIONS.find((x) => x.id === params.conversation_id);
      c.title = params.title;
      return { title: c.title };
    }
    case "tasks.submit":
    case "tasks.next":
      return { reply: "Demo data: nothing runs here. Start the app with `npm run tauri dev` to use Foreman." };
    default:
      return {};
  }
}

export function fakeTransport() {
  let onMessage = () => {};
  return {
    start: async () => {},
    onMessage: async (cb) => {
      onMessage = cb;
      return () => {};
    },
    onClosed: async () => () => {},
    send: async (line) => {
      const msg = JSON.parse(line);
      let reply;
      try {
        reply = { jsonrpc: "2.0", id: msg.id, result: result(msg.method, msg.params || {}) };
      } catch (e) {
        reply = { jsonrpc: "2.0", id: msg.id, error: { code: -32000, message: e.message, data: { kind: e.kind } } };
      }
      setTimeout(() => onMessage(JSON.stringify(reply)), 30);
    },
  };
}
