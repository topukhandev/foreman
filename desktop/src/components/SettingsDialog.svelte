<script>
  // Settings, in sections: Foreman's own (the same list as `foreman config` and /config, saved to config.yaml at
  // once), models and effort per provider (`foreman models`), how the app looks, the projects Foreman knows and how
  // they relate, and what is running.
  import { onMount } from "svelte";
  import { app, call, isListed, loadConfig, loadProjects, newConversation, say, setTheme } from "../lib/app.svelte.js";
  import { THEMES } from "../lib/theme.js";
  import ModelPicker from "./ModelPicker.svelte";

  const SECTIONS = [["general", "General"], ["models", "Models"], ["appearance", "Appearance"], ["projects", "Projects"],
    ["about", "About"]];
  let saving = $state("");
  let picking = $state("");
  let scanning = $state(false);
  let dialog = $state();

  onMount(() => dialog?.focus());

  const settings = $derived(app.config?.settings || []);
  const providers = $derived(Object.entries(app.config?.providers || {}).filter(([, p]) => p.enabled));

  async function set(key, value) {
    saving = key;
    try {
      const res = await call("config.set", { key, value });
      await loadConfig();
      const s = res.setting;
      say(`${s.label}: ${s.kind === "toggle" ? (s.value ? "on" : "off") : (s.options.find((o) => o.value === s.value)?.label ?? s.value)}`);
    } catch (e) {
      say(e.message);
      await loadConfig();
    }
    saving = "";
  }

  function modelText(p) {
    return `${p.model && p.model !== "default" ? p.model : "CLI default"}${p.effort ? ` · effort ${p.effort}` : ""}`;
  }

  // one line per related project: the kinds of relation joined, the strongest evidence kept
  function related(p) {
    const by = new Map();
    for (const r of p.relations) {
      const g = by.get(r.other) || { other: r.other, types: new Set(), directions: new Set(), evidence: "", source: r.source };
      g.types.add(r.type);
      g.directions.add(r.direction);
      if (!g.evidence && r.evidence) g.evidence = r.evidence;
      if (r.source === "declared") g.source = "declared";
      by.set(r.other, g);
    }
    return [...by.values()].map((g) => ({ ...g, types: [...g.types].join(", "),
      arrow: g.directions.size > 1 ? "↔" : g.directions.has("out") ? "→" : "←" }));
  }

  async function scan() {
    scanning = true;
    try {
      const res = await call("projects.scan", {}, 120000);
      await loadProjects();
      say(`Found ${res.projects.length} project${res.projects.length === 1 ? "" : "s"}.`);
    } catch (e) {
      say(e.message);
    }
    scanning = false;
  }

  let sessions = $state({}); // project -> [{provider, session_id, updated_at, title}] once asked for
  async function loadSessions(name) {
    try {
      sessions[name] = (await call("sessions.list", { project: name }, 60000)).sessions;
    } catch (e) {
      say(e.message);
    }
  }

  async function openIt(p) {
    try {
      await call("projects.open", { path: p.path });
      await loadProjects();
      app.settings = null;
      newConversation(p.path);
    } catch (e) {
      say(e.message);
    }
  }

  let doctor = $state(null);
  let checking = $state(false);
  async function check() {
    checking = true;
    try {
      doctor = await call("system.doctor", {}, 60000);
    } catch (e) {
      say(e.message);
    }
    checking = false;
  }
  let totals = $state("");
  $effect(() => {
    if (app.settings === "about" && !totals) call("system.stats", {}).then((r) => (totals = r.text)).catch(() => {});
  });

  function backdrop(e) {
    if (e.target === e.currentTarget) app.settings = null;
  }
</script>

<!-- svelte-ignore a11y_click_events_have_key_events -->
<div class="backdrop" role="presentation" onclick={backdrop}>
  <div class="dialog" role="dialog" aria-modal="true" aria-label="Settings" tabindex="-1" bind:this={dialog}>
    <nav>
      <div class="muted small head">Settings</div>
      {#each SECTIONS as [id, label]}
        <button class:on={app.settings === id} onclick={() => (app.settings = id)}>{label}</button>
      {/each}
      <div class="grow"></div>
      <div class="muted small keys">Ctrl+N new conversation<br />Ctrl+K message box<br />Ctrl+B sidebar · Ctrl+, settings</div>
    </nav>
    <div class="content">
      <button class="close" onclick={() => (app.settings = null)} aria-label="Close" title="Close (Esc)">×</button>

      {#if app.settings === "general"}
        <h2>General</h2>
        <p class="muted lead">How Foreman works. The same settings as <code>foreman config</code> in a terminal; changes are saved right away.</p>
        <div class="group">
          {#each settings.filter((s) => s.applies !== "chat") as s (s.key)}
            <div class="item">
              <div class="label"><label for="s-{s.key}">{s.label}</label>
                {#if s.kind === "choice"}<div class="muted small">{s.options.find((o) => o.value === s.value)?.description || ""}</div>{/if}
              </div>
              {#if s.kind === "toggle"}
                <label class="switch"><input id="s-{s.key}" type="checkbox" role="switch" checked={s.value} disabled={saving === s.key}
                  onchange={(e) => set(s.key, e.currentTarget.checked)} /><span aria-hidden="true"></span></label>
              {:else}
                <select id="s-{s.key}" value={s.value} disabled={saving === s.key} onchange={(e) => set(s.key, e.currentTarget.value)}>
                  {#each s.options as o}<option value={o.value} disabled={!o.available}>{o.label}{o.available ? "" : " (not installed)"}</option>{/each}
                </select>
              {/if}
            </div>
          {/each}
        </div>
        <h3>Terminal chat</h3>
        <div class="group">
          {#each settings.filter((s) => s.applies === "chat") as s (s.key)}
            <div class="item">
              <label class="label" for="s-{s.key}">{s.label}</label>
              <label class="switch"><input id="s-{s.key}" type="checkbox" role="switch" checked={s.value} disabled={saving === s.key}
                onchange={(e) => set(s.key, e.currentTarget.checked)} /><span aria-hidden="true"></span></label>
            </div>
          {/each}
        </div>

      {:else if app.settings === "models"}
        <h2>Models</h2>
        <p class="muted lead">The model and effort each provider uses. The list comes from each CLI; light steps and planning run one effort level lower.</p>
        <div class="group">
          {#each providers as [name, p] (name)}
            <div class="item col">
              <div class="row">
                <strong>{name}</strong>
                {#if !p.available}<span class="badge warn">not installed</span>{/if}
                <span class="grow muted">{modelText(p)}</span>
                <button disabled={!p.available} onclick={() => (picking = picking === name ? "" : name)}>{picking === name ? "Close" : "Change"}</button>
              </div>
              {#if picking === name}
                <ModelPicker provider={name} onDone={async (msg) => { picking = ""; await loadConfig(); if (msg) say(msg); }} />
              {/if}
            </div>
          {/each}
        </div>

      {:else if app.settings === "appearance"}
        <h2>Appearance</h2>
        <p class="muted lead">For this app on this computer.</p>
        <div class="group">
          <div class="item">
            <div class="label">Theme</div>
            <div class="segmented" role="radiogroup" aria-label="Theme">
              {#each THEMES as [value, label]}
                <button role="radio" aria-checked={app.theme === value} class:on={app.theme === value} onclick={() => setTheme(value)}>{label}</button>
              {/each}
            </div>
          </div>
        </div>

      {:else if app.settings === "projects"}
        <div class="row"><h2 class="grow">Projects</h2><button onclick={scan} disabled={scanning}>{scanning ? "Scanning…" : "Scan again"}</button></div>
        <p class="muted lead">Every project Foreman knows (found under your folders or opened by you), and how they depend
          on each other. Foreman uses this to include the right projects in a task. The sidebar lists only the ones you opened.</p>
        {#each app.projects as p (p.name)}
          {@const rel = related(p)}
          <details class="project">
            <summary><strong>{p.name}</strong> {#each p.stack as s}<span class="badge">{s}</span> {/each}
              <span class="muted small">{rel.length ? `${rel.length} related` : ""}</span></summary>
            <div class="muted small">{p.path}{p.test ? ` · tests: ${p.test}` : ""}</div>
            <div class="row actions">
              <button onclick={() => openIt(p)}>{isListed(p) ? "New conversation here" : "Open"}</button>
              {#if !sessions[p.name]}<button class="link" onclick={() => loadSessions(p.name)}>AI sessions</button>{/if}
            </div>
            {#if sessions[p.name]}
              <div class="muted small">AI sessions in this folder (Foreman reuses them when it helps):</div>
              <ul>{#each sessions[p.name].slice(0, 8) as x}<li><span class="muted">{x.provider}</span> {x.title || x.session_id}</li>
              {:else}<li class="muted">None found.</li>{/each}</ul>
            {/if}
            {#if rel.length}
              <ul>{#each rel as r}<li>{r.arrow} <strong>{r.other}</strong> <span class="muted">{r.types}{r.source === "declared" ? " · declared by you" : ""}</span>
                {#if r.evidence}<div class="muted small ev">{r.evidence}</div>{/if}</li>{/each}</ul>
            {/if}
          </details>
        {:else}<p class="muted">No projects found yet.</p>{/each}

      {:else}
        <h2>About</h2>
        <div class="group">
          <div class="item"><span class="label">Foreman</span><span>{app.hello?.core_version} · API {app.hello?.api_version}</span></div>
          <div class="item"><span class="label">Settings file</span><code>{app.config?.config_file}</code></div>
          <div class="item"><span class="label">Foreman home</span><code>{app.hello?.home}</code></div>
          <div class="item"><span class="label">Planner</span><span>{app.config?.planner}</span></div>
          {#each Object.entries(app.config?.providers || {}) as [name, p]}
            <div class="item"><span class="label">{name}</span><span class="muted">{!p.enabled ? "off" : p.available ? modelText(p) : p.info || "not installed"}</span></div>
          {/each}
        </div>
        <h3>Setup</h3>
        <div class="group">
          <div class="item col">
            <div class="row"><span class="grow">Providers, display, notifications and MCP registration (<code>foreman doctor</code>)</span>
              <button onclick={check} disabled={checking}>{checking ? "Checking…" : "Check setup"}</button></div>
            {#if doctor}
              <ul class="doctor">
                {#each Object.entries(doctor.providers) as [name, p]}
                  <li><span class={p.available ? "pass" : "fail"}>{p.available ? "✓" : "✗"}</span> {name} <span class="muted">{p.info}{p.available ? ` · runs today ${p.runs_today}/${p.max_runs_per_day}` : ""}</span></li>
                {/each}
                <li>Projects: {doctor.projects} · relations: {doctor.relations}</li>
                {#each Object.entries(doctor.mcp || {}) as [tool, st]}<li>MCP in {tool}: <span class="muted">{st}</span></li>{/each}
              </ul>
            {/if}
          </div>
        </div>
        {#if totals}
          <h3>Totals</h3>
          <pre class="totals">{totals}</pre>
        {/if}
      {/if}
    </div>
  </div>
</div>

<style>
  .backdrop { position: fixed; inset: 0; background: rgb(0 0 0 / .4); display: grid; place-items: center; padding: 24px; z-index: 40; }
  .dialog { width: min(920px, 100%); height: min(680px, 100%); background: var(--panel); border-radius: 14px; display: grid;
    grid-template-columns: 190px 1fr; overflow: hidden; box-shadow: 0 20px 60px rgb(0 0 0 / .3); outline: none; }
  nav { background: var(--sunken); padding: 16px 10px; display: flex; flex-direction: column; gap: 2px; }
  nav .head { padding: 0 10px 8px; }
  nav button { border: none; background: none; text-align: left; padding: 7px 10px; border-radius: 6px; }
  nav button:hover { background: var(--panel); }
  nav button.on { background: var(--panel); font-weight: 600; }
  .keys { padding: 0 10px; line-height: 1.7; }
  .content { overflow-y: auto; padding: 24px 32px 40px; position: relative; }
  .close { position: absolute; top: 14px; right: 16px; border: none; background: none; font-size: 20px; color: var(--muted); }
  h2 { font-size: 20px; margin-bottom: 4px; }
  h3 { margin: 22px 0 8px; }
  .lead { margin: 0 0 18px; max-width: 620px; }
  .small { font-size: 12.5px; }
  .group { border: 1px solid var(--line); border-radius: 10px; }
  .item { display: flex; gap: 16px; align-items: center; justify-content: space-between; padding: 12px 16px; }
  .item.col { flex-direction: column; align-items: stretch; gap: 8px; }
  .item + .item { border-top: 1px solid var(--line); }
  .label { min-width: 0; }
  select { max-width: 50%; }
  .switch { position: relative; width: 40px; height: 22px; flex: none; }
  .switch input { position: absolute; inset: 0; opacity: 0; margin: 0; cursor: pointer; }
  .switch span { position: absolute; inset: 0; border-radius: 999px; background: var(--line); transition: background .15s; pointer-events: none; }
  .switch span::after { content: ""; position: absolute; top: 3px; left: 3px; width: 16px; height: 16px; border-radius: 50%;
    background: var(--panel); transition: transform .15s; }
  .switch input:checked + span { background: var(--accent); }
  .switch input:checked + span::after { transform: translateX(18px); }
  .switch input:focus-visible + span { outline: 2px solid var(--accent); outline-offset: 2px; }
  .segmented { display: inline-flex; border: 1px solid var(--line); border-radius: 8px; padding: 2px; gap: 2px; }
  .segmented button { border: none; border-radius: 6px; padding: 4px 12px; background: none; color: var(--muted); }
  .segmented button.on { background: var(--sunken); color: var(--text); font-weight: 600; }
  .project { border: 1px solid var(--line); border-radius: 8px; padding: 8px 12px; }
  .project + .project { margin-top: 6px; }
  .project summary { cursor: pointer; }
  .project ul { margin: 8px 0 4px; padding-left: 18px; list-style: none; }
  .project li + li { margin-top: 6px; }
  .ev { overflow-wrap: anywhere; }
  .actions { margin: 8px 0 2px; gap: 8px; }
  .link { border: none; background: none; color: var(--muted); padding: 2px 4px; }
  .link:hover { color: var(--text); text-decoration: underline; }
  .doctor { margin: 4px 0 0; padding-left: 4px; list-style: none; font-size: 13.5px; }
  .doctor li + li { margin-top: 3px; }
  .pass { color: var(--ok); font-weight: 600; }
  .fail { color: var(--bad); font-weight: 600; }
  .totals { background: var(--sunken); border-radius: 8px; padding: 10px 12px; white-space: pre-wrap; font-size: 12.5px; margin: 0; }
  @media (max-width: 760px) {
    .dialog { grid-template-columns: 1fr; grid-template-rows: auto 1fr; }
    nav { flex-direction: row; overflow-x: auto; padding: 8px; }
    nav .head, nav .grow, .keys { display: none; }
    .content { padding: 20px 18px 32px; }
  }
</style>
