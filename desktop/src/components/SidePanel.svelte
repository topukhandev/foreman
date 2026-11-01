<script>
  // Right: everything a task did (its event timeline) and the raw output of each worker run, for when the few lines
  // in the conversation are not enough.
  import { app } from "../lib/app.svelte.js";
  import { isLive } from "../lib/tasks.js";
  import { clockTime } from "../lib/events.js";
  import { plain } from "../lib/render.js";
  import OutputView from "./OutputView.svelte";

  const id = $derived(app.panel?.taskId);
  const task = $derived(app.thread?.tasks?.[id] || app.tasks.find((t) => t.id === id));
  const lines = $derived(app.events[id] || []);
  let list = $state();
  $effect(() => {
    lines.length;
    if (list) list.scrollTop = list.scrollHeight;
  });
</script>

<aside class="panel">
  <header class="row">
    <div class="tabs row" role="tablist">
      <button role="tab" aria-selected={app.panel.tab === "activity"} class:on={app.panel.tab === "activity"}
              onclick={() => (app.panel.tab = "activity")}>Activity</button>
      <button role="tab" aria-selected={app.panel.tab === "output"} class:on={app.panel.tab === "output"}
              onclick={() => (app.panel.tab = "output")}>Raw output</button>
    </div>
    <span class="grow muted small title">#{id} {plain(task?.title || "")}</span>
    <button class="x" onclick={() => (app.panel = null)} aria-label="Close" title="Close (Esc)">×</button>
  </header>
  <div class="body">
    {#if app.panel.tab === "activity"}
      <ol class="timeline" bind:this={list}>
        {#each lines as l}<li><span class="time">{clockTime({ ts: l.at })}</span><span>{plain(l.text)}</span></li>
        {:else}<li class="muted">No activity yet.</li>{/each}
      </ol>
    {:else}
      {#key id}<OutputView taskId={id} live={task ? isLive(task) : false} />{/key}
    {/if}
  </div>
</aside>

<style>
  .panel { display: flex; flex-direction: column; min-height: 0; border-left: 1px solid var(--line); background: var(--panel); }
  header { padding: 10px 12px; border-bottom: 1px solid var(--line); gap: 8px; flex-wrap: nowrap; min-height: 52px; }
  .tabs { gap: 2px; flex: none; }
  .tabs button { border: none; background: none; padding: 4px 10px; border-radius: 6px; font-size: 13px; color: var(--muted); }
  .tabs button.on { background: var(--sunken); color: var(--text); font-weight: 600; }
  .title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .small { font-size: 12.5px; }
  .x { border: none; background: none; font-size: 18px; padding: 0 6px; color: var(--muted); }
  .body { flex: 1; min-height: 0; overflow: auto; padding: 10px 12px 24px; }
  .timeline { list-style: none; margin: 0; padding: 0; font-size: 12.5px; font-family: var(--mono); max-height: 100%; overflow: auto; }
  .timeline li { display: flex; gap: 8px; padding: 3px 0; border-bottom: 1px dashed var(--line); overflow-wrap: anywhere; }
  .time { color: var(--muted); flex: none; font-variant-numeric: tabular-nums; }
</style>
