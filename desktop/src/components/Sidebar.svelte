<script>
  // Left: a new conversation (the folder picker opens a project), what needs you, and each opened project with its
  // conversations (newest first). Only projects the user opened are listed (in the app or the CLI: `foreman open`,
  // the chat started in it, a request sent from it); tasks started outside a conversation show under their project.
  import { app, conversationOf, folderOf, hideProject, isListed, newConversation, openConversation, openProject,
    openTask } from "../lib/app.svelte.js";
  import { ago, isLive, needsYou } from "../lib/tasks.js";
  import { plain } from "../lib/render.js";

  let { onCollapse } = $props();

  const LOOSE_DAYS = 3;
  let expanded = $state(load());

  function load() {
    try {
      return JSON.parse(localStorage.getItem("foreman.expanded") || "{}");
    } catch {
      return {};
    }
  }
  function toggle(name) {
    expanded[name] = !isOpen(name);
    try {
      localStorage.setItem("foreman.expanded", JSON.stringify(expanded));
    } catch {}
  }

  const taskById = $derived(Object.fromEntries(app.tasks.map((t) => [t.id, t])));
  const needs = $derived(app.tasks.filter((t) => needsYou(t)).slice(0, 8));
  const current = $derived(app.open.kind === "conversation" ? app.open.project
    : app.open.kind === "task" ? taskById[app.open.id]?.project : null);

  // projects with their conversations and loose tasks, the most recently active first
  const groups = $derived.by(() => {
    const byName = new Map();
    const group = (name) => {
      if (!byName.has(name)) {
        const p = app.projects.find((x) => x.name === name);
        byName.set(name, { name, label: p ? p.name : String(name).split(/[\\/]/).filter(Boolean).pop() || name,
          path: p?.path || name, conversations: [], loose: [], latest: typeof p?.opened === "number" ? p.opened : 0 });
      }
      return byName.get(name);
    };
    for (const p of app.projects) if (isListed(p)) group(p.name);
    const listed = new Set(byName.keys());
    const hidden = new Set(app.projects.filter((p) => p.opened === false).map((p) => p.name));
    for (const c of app.conversations) {
      if (hidden.has(c.project) || (!listed.has(c.project) && app.projects.some((p) => p.name === c.project))) continue;
      const g = group(c.project);
      g.conversations.push(c);
      g.latest = Math.max(g.latest, c.updated_at || 0);
    }
    const cutoff = Date.now() / 1000 - LOOSE_DAYS * 86400;
    for (const t of app.tasks) {
      if (!t.project || conversationOf(t.id)) continue;
      const when = Date.parse(String(t.updated_at).replace(" ", "T")) / 1000;
      if (!(isLive(t) || needsYou(t) || when > cutoff) || !listed.has(t.project)) continue;
      const g = group(t.project);
      g.loose.push(t);
      g.latest = Math.max(g.latest, when || 0);
    }
    return [...byName.values()].sort((a, b) => b.latest - a.latest || a.label.localeCompare(b.label));
  });


  function isOpen(name) {
    return expanded[name] ?? (name === current || groups.find((g) => g.name === name)?.latest > Date.now() / 1000 - 86400);
  }

  function activity(c) {
    const ts = c.tasks.map((id) => taskById[id]).filter(Boolean);
    if (ts.some(isLive)) return "running";
    if (ts.some((t) => needsYou(t))) return "needs";
    return "";
  }
</script>

<aside class="sidebar">
  <div class="top row">
    <img class="logo" src="/icon.png" alt="" width="26" height="26" />
    <strong class="grow">Foreman</strong>
    <button class="icon" onclick={onCollapse} title="Hide the sidebar (Ctrl+B)" aria-label="Hide the sidebar">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v14H4zM9 5v14" /></svg>
    </button>
  </div>

  <button class="new" onclick={() => openProject()} title="Open a project folder and start a conversation there (Ctrl+N)">
    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg> New conversation
  </button>

  <div class="scroll">
    {#if needs.length}
      <div class="section">Needs you</div>
      {#each needs as t (t.id)}
        <button class="item needs" class:on={app.open.kind === "task" && app.open.id === t.id} onclick={() => openTask(t.id)}>
          <span class="dot warn" aria-hidden="true"></span>
          <span class="grow title">{plain(t.title)}</span>
          <span class="meta">{needsYou(t)}</span>
        </button>
      {/each}
    {/if}

    <div class="section">Projects</div>
    {#if !groups.length}<p class="hint muted">Open a project folder with New conversation; it stays listed here.</p>{/if}
    {#each groups as g (g.name)}
      <div class="project">
        <button class="item folder" onclick={() => toggle(g.name)} aria-expanded={isOpen(g.name)} title={g.path}>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d={isOpen(g.name) ? "M3 7h6l2 2h10v10H3z M3 11h18" : "M3 7h6l2 2h10v10H3z"} /></svg>
          <span class="grow title">{g.label}</span>
          {#if !isOpen(g.name) && (g.conversations.some((c) => activity(c)) || g.loose.length)}<span class="dot live" aria-label="activity"></span>{/if}
        </button>
        <span class="tools">
          <button onclick={() => newConversation(folderOf(g.name))} title="New conversation in {g.label}"
                  aria-label="New conversation in {g.label}">+</button>
          {#if app.projects.some((p) => p.name === g.name)}
            <button onclick={() => hideProject(g.name)} title="Hide {g.label} from this list" aria-label="Hide {g.label}">×</button>
          {/if}
        </span>
      </div>
      {#if isOpen(g.name)}
        {#each g.conversations as c (c.id)}
          {@const s = activity(c)}
          <button class="item child" class:on={app.open.kind === "conversation" && app.open.id === c.id}
                  onclick={() => openConversation(c.project, c.id)}>
            <span class="grow title">{plain(c.title) || "Untitled"}</span>
            {#if s === "running"}<span class="spin" aria-label="running"></span>
            {:else if s === "needs"}<span class="dot warn" aria-label="needs you"></span>
            {:else}<span class="meta">{ago(c.updated_at).replace(" ago", "")}</span>{/if}
          </button>
        {/each}
        {#each g.loose as t (t.id)}
          <button class="item child task" class:on={app.open.kind === "task" && app.open.id === t.id} onclick={() => openTask(t.id)}
                  title="Started outside a conversation (#{t.id})">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01" /></svg>
            <span class="grow title">{plain(t.title)}</span>
            {#if isLive(t)}<span class="spin" aria-label="running"></span>{:else}<span class="meta">{ago(t.updated_at).replace(" ago", "")}</span>{/if}
          </button>
        {/each}
        {#if !g.conversations.length && !g.loose.length}
          <button class="item child empty" onclick={() => newConversation(folderOf(g.name))}>Start a conversation</button>
        {/if}
      {/if}
    {/each}
  </div>

  <div class="bottom">
    <button class="item" onclick={() => (app.settings = "general")} title="Ctrl+,">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z M4 12h2M18 12h2M12 4v2M12 18v2M6.3 6.3l1.4 1.4M16.3 16.3l1.4 1.4M6.3 17.7l1.4-1.4M16.3 7.7l1.4-1.4" /></svg>
      <span class="grow">Settings</span>
      <span class="conn" title={app.connected ? (app.demo ? "Demo data" : "Connected to Foreman") : app.connecting ? "Starting Foreman…" : "Not connected"}>
        <span class="dot" class:ok={app.connected} class:warn={app.connecting && !app.connected} class:bad={!app.connected && !app.connecting}></span>
      </span>
    </button>
  </div>
</aside>

<style>
  .sidebar { display: flex; flex-direction: column; min-height: 0; background: var(--panel); border-right: 1px solid var(--line); }
  .top { padding: 14px 12px 10px 16px; gap: 8px; }
  .logo { width: 26px; height: 26px; border-radius: 6px; }
  .icon { border: none; background: none; padding: 4px; color: var(--muted); }
  svg { width: 17px; height: 17px; flex: none; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
  .new { margin: 4px 12px 8px; display: flex; gap: 8px; align-items: center; padding: 8px 12px; font-weight: 500; }
  .scroll { flex: 1; overflow-y: auto; padding: 0 8px 12px; }
  .section { font-size: 12px; color: var(--muted); padding: 14px 8px 4px; }
  .item { display: flex; gap: 8px; align-items: center; width: 100%; border: none; background: none; text-align: left;
    padding: 6px 8px; border-radius: 6px; color: var(--text); min-height: 32px; }
  .item:hover { background: var(--sunken); }
  .item.on { background: var(--sunken); font-weight: 600; }
  .folder { color: var(--muted); }
  .folder:hover { color: var(--text); }
  .project { position: relative; }
  .tools { position: absolute; right: 4px; top: 4px; display: flex; gap: 2px; opacity: 0; }
  .tools button { border: none; background: none; padding: 0 6px; font-size: 16px; line-height: 24px; color: var(--muted); border-radius: 4px; }
  .project:hover .tools, .tools:focus-within { opacity: 1; }
  .tools button:hover { color: var(--text); background: var(--panel); }
  .child { padding-left: 33px; font-size: 13.5px; }
  .child.task { padding-left: 26px; color: var(--muted); }
  .child.task svg { width: 14px; height: 14px; }
  .child.empty { color: var(--muted); font-style: italic; }
  .title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .meta { font-size: 12px; color: var(--muted); white-space: nowrap; font-weight: 400; }
  .dot { width: 8px; height: 8px; border-radius: 50%; flex: none; background: var(--line); }
  .dot.warn { background: var(--warn); }
  .dot.live { background: var(--accent); }
  .dot.ok { background: var(--ok); }
  .dot.bad { background: var(--bad); }
  .spin { width: 10px; height: 10px; border-radius: 50%; border: 2px solid var(--accent); border-right-color: transparent;
    animation: spin .9s linear infinite; flex: none; }
  @keyframes spin { to { transform: rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
  .bottom { border-top: 1px solid var(--line); padding: 8px; }
  .hint { font-size: 12.5px; padding: 2px 8px; margin: 0; }
  .conn { display: flex; }
</style>
