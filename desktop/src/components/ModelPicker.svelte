<script>
  // A provider's model and effort, from what its CLI lists (models.list), like `foreman models`: search by words
  // (each a regex, all must match), browse one backend at a time when there are several, or type a name that isn't
  // listed. Effort offers only the levels the chosen model supports.
  import { call } from "../lib/app.svelte.js";
  import { searchModels } from "../lib/tasks.js";

  let { provider, onDone } = $props();
  const SHOWN = 200;

  let data = $state(null);
  let error = $state("");
  let query = $state("");
  let group = $state("");
  let model = $state(""); // "default", a listed id, or "" (typed: see `typed`)
  let typed = $state("");
  let effort = $state("");
  let saving = $state(false);

  $effect(() => {
    load();
  });

  async function load() {
    try {
      data = await call("models.list", { provider }, 60000);
      const cur = data.current.model;
      model = !cur || cur === "default" ? "default" : data.models.some((m) => m.id === cur) ? cur : "";
      if (model === "" && cur && cur !== "default") typed = cur;
      effort = data.current.effort || "";
    } catch (e) {
      error = e.message;
    }
  }

  const groups = $derived(data ? [...new Set(data.models.map((m) => m.group).filter(Boolean))].sort() : []);
  const found = $derived(data ? searchModels(data.models.filter((m) => !group || m.group === group), query) : { models: [], bad: false });
  const levels = $derived(
    !data ? [] : model === "default" ? [] : model ? data.models.find((m) => m.id === model)?.levels || [] : typed.trim() ? data.typed_levels : []);

  $effect(() => {
    if (levels.length && !levels.includes(effort)) effort = levels.includes("high") ? "high" : levels[levels.length - 1];
    if (!levels.length) effort = "";
  });

  async function save() {
    const name = model || typed.trim();
    if (!name) return;
    saving = true;
    try {
      const res = await call("models.set", { provider, model: name, effort: effort || null });
      onDone(`${provider}: ${res.model === "default" ? "CLI default" : res.model}${res.effort ? ` · effort ${res.effort}` : ""}`);
    } catch (e) {
      error = e.message;
    }
    saving = false;
  }
</script>

<div class="picker stack">
  {#if error}<p class="bad">{error}</p>{/if}
  {#if !data && !error}
    <p class="muted">Asking {provider} which models it offers…</p>
  {:else if data}
    <div class="row">
      <input class="grow" bind:value={query} placeholder="Search models (words, each a regex: deepseek free, qwen.*coder)"
             aria-label="Search models" />
      {#if groups.length > 1}
        <select bind:value={group} aria-label="Backend">
          <option value="">All backends ({data.models.length})</option>
          {#each groups as g}<option value={g}>{g}</option>{/each}
        </select>
      {/if}
    </div>
    {#if found.bad}<p class="muted small">"{query}" is not a valid regex.</p>{/if}
    <div class="models" role="radiogroup" aria-label="Model">
      <label class="model"><input type="radio" bind:group={model} value="default" /> <span>CLI default <span class="muted">(let {provider} decide)</span></span></label>
      {#each found.models.slice(0, SHOWN) as m (m.id)}
        <label class="model">
          <input type="radio" bind:group={model} value={m.id} />
          <span class="grow name"><code>{m.id}</code>{#if m.name && m.name !== m.id}<span class="muted">{m.name}</span>{/if}</span>
          {#if m.levels.length}<span class="muted small">{m.levels.join(", ")}</span>{/if}
        </label>
      {/each}
      {#if found.models.length > SHOWN}<p class="muted small">{found.models.length - SHOWN} more: narrow the search.</p>{/if}
      {#if !data.models.length}<p class="muted small">{provider} can't list its models. Type a name below, or keep its CLI default.</p>{/if}
    </div>
    <div class="row">
      <label class="row typed"><input type="radio" bind:group={model} value="" /> Other:</label>
      <input class="grow" bind:value={typed} onfocus={() => (model = "")} placeholder="a model name that isn't listed" aria-label="Model name" />
    </div>
    <div class="row">
      {#if levels.length}
        <label for="effort-{provider}">Effort</label>
        <select id="effort-{provider}" bind:value={effort}>
          {#each levels as lv}<option value={lv}>{lv}</option>{/each}
        </select>
        <span class="muted small">Higher is slower and uses more of your limits; light steps and planning run one level lower.</span>
      {/if}
      <span class="grow"></span>
      <button onclick={() => onDone("")}>Cancel</button>
      <button class="primary" disabled={saving || (!model && !typed.trim())} onclick={save}>{saving ? "Saving…" : "Use this"}</button>
    </div>
  {/if}
</div>

<style>
  .picker { background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 10px; }
  .models { max-height: 280px; overflow: auto; border: 1px solid var(--line); border-radius: 6px; padding: 4px; }
  .model { display: flex; gap: 8px; align-items: center; padding: 4px 6px; border-radius: 4px; cursor: pointer; }
  .model:hover { background: var(--sunken); }
  .typed { gap: 4px; white-space: nowrap; }
  .name { display: flex; gap: 8px; align-items: baseline; min-width: 0; }
  .small { font-size: 12.5px; }
  .bad { color: var(--bad); margin: 0; }
</style>
