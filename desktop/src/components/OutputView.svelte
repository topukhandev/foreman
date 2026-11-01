<script>
  // Raw output of a provider run (tasks.outputs / tasks.output): redacted when it was written, stripped of terminal
  // control sequences by the API, shown as plain text only. The view keeps the last MAX_LINES lines.
  import { call } from "../lib/app.svelte.js";
  import { plain } from "../lib/render.js";

  let { taskId, live = false } = $props();
  const MAX_LINES = 5000;

  let outputs = $state([]);
  let name = $state("");
  let lines = $state([]);
  let offset = 0;
  let error = $state("");

  async function list() {
    try {
      outputs = (await call("tasks.outputs", { task_id: taskId })).outputs;
      if (!name && outputs.length) choose(outputs[outputs.length - 1].name);
    } catch (e) {
      error = e.message;
    }
  }

  function choose(n) {
    name = n;
    lines = [];
    offset = 0;
    read();
  }

  async function read() {
    if (!name) return;
    try {
      for (let i = 0; i < 20; i++) {
        const page = await call("tasks.output", { task_id: taskId, name, offset, limit: 65536 });
        offset = page.offset;
        if (page.text) lines = [...lines, ...plain(page.text).split("\n").filter((l, j, a) => j < a.length - 1 || l)].slice(-MAX_LINES);
        if (page.eof) break;
      }
    } catch (e) {
      error = e.message;
    }
  }

  $effect(() => {
    list();
    if (!live) return;
    const timer = setInterval(() => (name ? read() : list()), 1000);
    const lister = setInterval(list, 5000);
    return () => {
      clearInterval(timer);
      clearInterval(lister);
    };
  });
</script>

<div class="stack">
  {#if outputs.length}
    <select value={name} onchange={(e) => choose(e.currentTarget.value)} aria-label="Run">
      {#each outputs as o}<option value={o.name}>{o.name} ({Math.round(o.size / 1024)} KB)</option>{/each}
    </select>
    <pre class="out">{lines.join("\n")}</pre>
  {:else}
    <p class="muted">{error || "No output recorded for this task."}</p>
  {/if}
</div>

<style>
  .out { background: var(--sunken); padding: 10px 12px; border-radius: 6px; overflow: auto;
    white-space: pre-wrap; overflow-wrap: anywhere; margin: 0; }
</style>
