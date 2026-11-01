<script>
  // D3: closing the window while tasks run asks "keep running or stop all" (unless a choice was saved). Tasks are
  // detached runners: keeping them running is the safe default; "stop all" sends tasks.stop for each one.
  import { onMount } from "svelte";
  import { app, call, loadTasks, runningTasks } from "../lib/app.svelte.js";
  import { exitApp } from "../lib/shell.js";

  let remember = $state(false);
  let busy = $state(false);
  let running = $state([]);
  let ready = $state(false);
  let keepButton = $state();

  onMount(async () => {
    try {
      await loadTasks();
    } catch {}
    running = runningTasks();
    const pref = app.config?.on_close || "ask";
    if (!running.length || pref === "keep" || !app.connected) return finish(false);
    if (pref === "stop") return finish(true);
    ready = true;
    queueMicrotask(() => keepButton?.focus());
  });

  async function finish(stop) {
    busy = true;
    try {
      if (remember) await call("config.set_on_close", { value: stop ? "stop" : "keep" });
      if (stop) await Promise.allSettled(running.map((t) => call("tasks.stop", { task_id: t.id })));
    } finally {
      await exitApp();
      busy = false;
    }
  }

  function keys(e) {
    if (e.key === "Escape") app.closing = false;
  }
</script>

{#if ready}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="backdrop" onkeydown={keys}>
    <div class="dialog card stack" role="dialog" aria-modal="true" aria-labelledby="close-title">
      <h2 id="close-title">{running.length} task{running.length === 1 ? " is" : "s are"} running</h2>
      <p>They run on their own and can keep going after the app closes. You'll see where they got to next time.</p>
      <ul class="muted">{#each running.slice(0, 5) as t}<li>#{t.id} {t.title}</li>{/each}</ul>
      <label class="row"><input type="checkbox" bind:checked={remember} /> Remember my choice (change it in Settings)</label>
      <div class="row end">
        <button onclick={() => (app.closing = false)} disabled={busy}>Don't close</button>
        <button class="danger" onclick={() => finish(true)} disabled={busy}>Stop all and close</button>
        <button class="primary" bind:this={keepButton} onclick={() => finish(false)} disabled={busy}>Keep running and close</button>
      </div>
    </div>
  </div>
{/if}

<style>
  .backdrop { position: fixed; inset: 0; background: rgb(0 0 0 / .45); display: grid; place-items: center; padding: 16px; }
  .dialog { max-width: 480px; width: 100%; }
  .end { justify-content: flex-end; }
  ul { margin: 0; padding-left: 18px; }
</style>
