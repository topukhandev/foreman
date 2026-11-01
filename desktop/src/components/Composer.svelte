<script>
  // The message box at the bottom: Enter sends, Shift+Enter starts a new line. The same message sent twice (a double
  // click, a retry after a lost reply) carries the same idempotency key, so it acts once.
  import { app, send } from "../lib/app.svelte.js";

  let { placeholder = "Ask anything, or describe a change", disabled = false } = $props();
  const MODES = [["", "Auto mode"], ["research", "Research"], ["planning", "Planning"], ["development", "Development"],
    ["qa", "QA"], ["deployment", "Deployment"]];

  let text = $state("");
  let mode = $state("");
  let dryRun = $state(false);
  let box = $state();
  let key = null;
  let keyFor = "";

  const busy = $derived(!!app.pending);

  $effect(() => {
    if (app.draft) {
      text = app.draft;
      app.draft = "";
      box?.focus();
    }
  });
  $effect(() => {
    text; // grow with the text, up to a limit
    if (!box) return;
    box.style.height = "auto";
    box.style.height = Math.min(box.scrollHeight, 220) + "px";
  });

  async function submit(e) {
    e?.preventDefault();
    const message = text.trim();
    if (!message || busy || disabled) return;
    const what = JSON.stringify([message, mode, dryRun, app.open]);
    if (what !== keyFor) {
      key = crypto.randomUUID();
      keyFor = what;
    }
    text = "";
    const res = await send(message, { mode: mode || null, dryRun, key });
    if (res) keyFor = "";
    else if (!text) text = message; // not sent: keep it to try again
  }

  function keys(e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) submit(e);
  }
</script>

<form class="composer" onsubmit={submit}>
  <textarea id="composer" bind:this={box} bind:value={text} rows="1" onkeydown={keys}
            disabled={!app.connected || disabled} {placeholder} aria-label="Message"></textarea>
  <div class="row tools">
    <select bind:value={mode} aria-label="Mode" title="Lifecycle mode (Auto: Foreman recommends one per request)">
      {#each MODES as [value, label]}<option {value}>{label}</option>{/each}
    </select>
    <label class="toggle" title="Plan the work and stop before anything changes; run the plan later">
      <input type="checkbox" bind:checked={dryRun} /> Plan only</label>
    <span class="grow"></span>
    <span class="hint muted">Enter to send · Shift+Enter for a new line</span>
    <button class="send" type="submit" disabled={!app.connected || busy || disabled || !text.trim()} aria-label="Send" title="Send (Enter)">
      {#if busy}<span class="spin" aria-hidden="true"></span>
      {:else}<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6" /></svg>{/if}
    </button>
  </div>
</form>

<style>
  .composer { border: 1px solid var(--line); border-radius: 14px; background: var(--panel); padding: 10px 10px 8px 14px;
    box-shadow: 0 2px 10px rgb(0 0 0 / .04); }
  .composer:focus-within { border-color: var(--accent); }
  textarea { width: 100%; border: none; background: transparent; padding: 2px 0 6px; outline: none; resize: none;
    min-height: 24px; max-height: 220px; line-height: 1.5; }
  textarea:focus-visible { outline: none; }
  .tools { gap: 6px; }
  select { border: none; background: var(--sunken); border-radius: 999px; padding: 3px 10px; font-size: 12.5px; color: var(--muted); }
  .toggle { display: flex; gap: 5px; align-items: center; font-size: 12.5px; color: var(--muted); padding: 3px 8px;
    border-radius: 999px; background: var(--sunken); cursor: pointer; }
  .toggle input { margin: 0; }
  .hint { font-size: 11.5px; }
  .send { width: 32px; height: 32px; border-radius: 50%; padding: 0; display: grid; place-items: center;
    background: var(--accent); color: var(--accent-text); border: none; }
  .send:disabled { background: var(--sunken); color: var(--muted); opacity: 1; }
  .send svg { width: 17px; height: 17px; fill: none; stroke: currentColor; stroke-width: 2.2; stroke-linecap: round; stroke-linejoin: round; }
  .spin { width: 13px; height: 13px; border-radius: 50%; border: 2px solid currentColor; border-right-color: transparent; animation: spin .9s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  @media (max-width: 760px) { .hint { display: none; } }
</style>
