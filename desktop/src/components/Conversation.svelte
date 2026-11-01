<script>
  // The middle: a conversation's messages, each task shown where it was last talked about (so the live one is near the
  // bottom), and the message box. A new conversation starts with a project to work in.
  import { tick } from "svelte";
  import { app, call, isListed, loadConversations, openProject, say } from "../lib/app.svelte.js";
  import { isLive, needsYou } from "../lib/tasks.js";
  import { plain, terminalText } from "../lib/render.js";
  import Composer from "./Composer.svelte";
  import TaskCard from "./TaskCard.svelte";

  let scroller = $state();
  let renaming = $state(false);
  let newTitle = $state("");
  const listedProjects = $derived(app.projects.filter(isListed));

  const conv = $derived(app.thread?.conversation);
  const tasks = $derived(app.thread?.tasks || {});
  const project = $derived(app.open.kind === "conversation" ? app.open.project
    : app.open.kind === "task" ? tasks[app.open.id]?.project || "" : app.projects.find((p) => p.path === app.open.cwd)?.name || "");
  const label = $derived(String(project || "").split(/[\\/]/).filter(Boolean).pop() || project);

  // each task is shown once, at the last message that refers to it
  const rows = $derived.by(() => {
    if (app.open.kind === "task") {
      const t = tasks[app.open.id];
      return t ? [{ user: t.request, task: t.id, card: true, at: 0 }] : [];
    }
    const entries = conv?.entries || [];
    const last = {};
    entries.forEach((e, i) => e.task && (last[e.task] = i));
    return entries.map((e, i) => ({ ...e, card: !!e.task && last[e.task] === i }));
  });

  const focus = $derived(conv?.focus ? tasks[conv.focus] : app.open.kind === "task" ? tasks[app.open.id] : null);
  const asking = $derived(focus && (focus.questions || []).length > 0 && !needsYou(focus).startsWith("Stopped"));

  // keep the newest message in view when something is added and the reader is at the bottom
  let atBottom = true;
  let placed = 0; // when the view was last moved on purpose: its scroll events are not the reader's
  function scrolled() {
    if (scroller && Date.now() - placed > 400) atBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
  }
  $effect(() => {
    rows.length;
    app.pending;
    if (focus && isLive(focus)) JSON.stringify(app.activity[focus.id] || []); // a running task's latest lines
    if (atBottom) tick().then(toEnd);
  });
  // A conversation opens at its end; when its last task card is taller than the view, at that card's top.
  let opened = null;
  $effect(() => {
    // wait for the last task's details too: they make its card as tall as it will be
    const lastTask = rows.findLast?.((r) => r.card)?.task;
    if (!app.thread || opened === app.open || (lastTask && !app.details[lastTask])) return;
    opened = app.open;
    atBottom = true;
    tick().then(() => {
      toEnd();
      const cards = scroller?.querySelectorAll(".task");
      const last = cards?.[cards.length - 1];
      if (last && last.offsetHeight > scroller.clientHeight * 0.8) {
        placed = Date.now();
        scroller.scrollTop += last.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 12;
        atBottom = false;
      }
    });
  });
  function toEnd() {
    if (scroller) scroller.scrollTop = scroller.scrollHeight;
  }

  async function rename(e) {
    e?.preventDefault();
    renaming = false;
    if (!newTitle.trim() || newTitle.trim() === conv?.title) return;
    try {
      await call("conversations.rename", { project: app.open.project, conversation_id: app.open.id, title: newTitle.trim() });
      await loadConversations();
      app.thread.conversation.title = newTitle.trim();
    } catch (err) {
      say(err.message);
    }
  }

  const EXAMPLES = ["Where is the login flow, and what calls it?", "What's the state of my last task?",
    "Add input validation to the signup form", "What's next for this project?"];
  function example(text) {
    app.draft = text;
  }
</script>

<section class="conversation">
  <header class="row">
    {#if app.open.kind === "new"}
      {#if label}<span class="crumb muted">{label}</span><span class="sep">/</span>{/if}<span class="crumb">New conversation</span>
    {:else}
      <span class="crumb muted">{label}</span><span class="sep">/</span>
      {#if renaming}
        <form class="grow" onsubmit={rename}>
          <!-- svelte-ignore a11y_autofocus -->
          <input class="title-edit" bind:value={newTitle} onblur={rename} autofocus aria-label="Conversation name" />
        </form>
      {:else if app.open.kind === "conversation"}
        <button class="crumb title" onclick={() => { newTitle = conv?.title || ""; renaming = true; }} title="Rename">
          {plain(conv?.title) || "Untitled"}</button>
      {:else}
        <span class="crumb title">#{app.open.id} {plain(tasks[app.open.id]?.title || "")}</span>
      {/if}
    {/if}
    <span class="grow"></span>
    {#if focus}
      <button class="ghost" class:on={!!app.panel} onclick={() => (app.panel = app.panel ? null : { taskId: focus.id, tab: "activity" })}
              title="Activity and output of #{focus.id}">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v14H4zM15 5v14" /></svg>
      </button>
    {/if}
  </header>

  <div class="thread" bind:this={scroller} onscroll={scrolled}>
    <div class="column">
      {#if app.open.kind === "new"}
        <div class="welcome">
          <h1>What should Foreman do?</h1>
          <p class="muted">Ask about the code or describe a change. Foreman plans it, runs the AI workers, checks the result
            itself, and asks you when a decision is yours.</p>
          <div class="where row">
            {#if app.open.cwd}
              <span class="muted">In</span>
              <select value={app.open.cwd} title={app.open.cwd} aria-label="Project"
                      onchange={(e) => (e.currentTarget.value === "\u0000" ? (e.currentTarget.value = app.open.cwd, openProject())
                        : (app.open = { kind: "new", cwd: e.currentTarget.value }))}>
                {#if !listedProjects.some((p) => p.path === app.open.cwd)}<option value={app.open.cwd}>{label || app.open.cwd}</option>{/if}
                {#each listedProjects as p}<option value={p.path}>{p.name}</option>{/each}
                <option value={"\u0000"}>Open another folder…</option>
              </select>
            {:else}
              <button class="primary" onclick={() => openProject()}>Open a project folder</button>
            {/if}
          </div>
          <div class="examples">
            {#each EXAMPLES as e}<button onclick={() => example(e)}>{e}</button>{/each}
          </div>
        </div>
      {:else if !app.thread}
        <p class="muted loading">Loading…</p>
      {/if}

      {#each rows as row, i (i)}
        {#if row.user}
          <div class="you"><div class="bubble">{plain(row.shown || row.user)}</div></div>
        {/if}
        {#if row.task && row.card && tasks[row.task]}
          <TaskCard task={tasks[row.task]} />
        {:else if row.task && tasks[row.task]}
          <div class="ref muted">→ continued below: #{row.task} {plain(tasks[row.task].title)}</div>
        {:else if row.output}
          <div class="foreman"><div class="text">{terminalText(row.output)}</div></div>
        {/if}
      {/each}

      {#if app.pending}
        <div class="you"><div class="bubble pending">{app.pending.text}</div></div>
        <div class="thinking muted"><span class="spin" aria-hidden="true"></span> Foreman is on it…</div>
      {/if}
    </div>
  </div>

  <div class="bottom">
    <div class="column">
      <Composer placeholder={app.open.kind === "task" ? `Start a conversation in ${label}`
        : asking ? "Answer the question above, or ask something else" : "Ask anything, or describe a change"}
        disabled={app.open.kind === "new" && !app.open.cwd} />
    </div>
  </div>
</section>

<style>
  .conversation { display: flex; flex-direction: column; min-height: 0; flex: 1; }
  header { padding: 12px 20px 10px 24px; border-bottom: 1px solid var(--line); gap: 6px; min-height: 52px; flex-wrap: nowrap; }
  :global(.collapsed) header { padding-left: 52px; }
  .crumb { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .crumb.title { border: none; background: none; padding: 2px 4px; font-weight: 500; max-width: 60%; text-align: left; }
  button.crumb.title:hover { background: var(--sunken); }
  .sep { color: var(--muted); }
  .title-edit { width: 100%; max-width: 480px; }
  .ghost { border: none; background: none; padding: 5px; color: var(--muted); border-radius: 6px; }
  .ghost:hover, .ghost.on { background: var(--sunken); color: var(--text); }
  .ghost svg { width: 18px; height: 18px; display: block; fill: none; stroke: currentColor; stroke-width: 1.8; }
  .thread { flex: 1; overflow-y: auto; padding: 24px 24px 32px; }
  .column { max-width: 820px; margin: 0 auto; }
  .welcome { margin: 8vh auto 24px; max-width: 620px; display: flex; flex-direction: column; gap: 14px; }
  .welcome h1 { font-size: 24px; }
  .welcome p { margin: 0; }
  .where select { min-width: 220px; font-weight: 500; }
  .examples { display: flex; flex-wrap: wrap; gap: 8px; }
  .examples button { border-radius: 999px; font-size: 13px; color: var(--muted); }
  .examples button:hover { color: var(--text); }
  .loading { text-align: center; margin-top: 20vh; }
  .you { display: flex; justify-content: flex-end; margin: 18px 0 10px; }
  .bubble { max-width: 80%; background: var(--sunken); border: 1px solid var(--line); border-radius: 14px 14px 4px 14px;
    padding: 9px 14px; white-space: pre-wrap; overflow-wrap: anywhere; }
  .bubble.pending { opacity: .7; }
  .foreman .text { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.55; margin: 4px 0 6px; }
  .ref { font-size: 12.5px; margin: 0 0 6px; }
  .thinking { display: flex; gap: 8px; align-items: center; font-size: 13px; margin: 6px 0; }
  .spin { width: 11px; height: 11px; border-radius: 50%; border: 2px solid var(--accent); border-right-color: transparent;
    animation: spin .9s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .bottom { padding: 8px 24px 18px; }
  @media (max-width: 760px) {
    .thread { padding: 16px 16px 24px; }
    .bottom { padding: 6px 12px 12px; }
    header { padding-left: 52px; }
  }
</style>
