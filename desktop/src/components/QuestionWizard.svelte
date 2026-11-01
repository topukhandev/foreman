<script>
  // A task's open questions, one at a time: pick an option (or press its number), or type your own answer; Back and
  // Next move between them, and the answers go together at the end. Drafts are kept until they are sent.
  import { act } from "../lib/app.svelte.js";
  import { options as optionsOf } from "../lib/tasks.js";
  import { plain } from "../lib/render.js";
  import Markdown from "./Markdown.svelte";

  let { task, record = null, live = false } = $props();

  const questions = $derived(task.questions || []);
  const storeKey = $derived(`foreman.answers.${task.id}`);
  let answers = $state({});
  let at = $state(0);
  let busy = $state(false);
  let loadedFor = "";
  let key = null;

  $effect(() => {
    if (loadedFor === storeKey) return;
    loadedFor = storeKey;
    try {
      answers = JSON.parse(localStorage.getItem(storeKey) || "{}");
    } catch {
      answers = {};
    }
    at = Math.max(0, questions.findIndex((q) => !answers[q.id]));
  });
  $effect(() => {
    try {
      const kept = Object.fromEntries(Object.entries(answers).filter(([, v]) => v));
      if (Object.keys(kept).length) localStorage.setItem(storeKey, JSON.stringify(kept));
      else localStorage.removeItem(storeKey);
    } catch {}
  });

  const review = $derived(questions.length > 1 && at >= questions.length);
  const q = $derived(review ? null : questions[Math.min(at, questions.length - 1)]);
  const choices = $derived(!q ? [] : q.kind === "approval"
    ? [{ label: "Approve and run", value: "yes", description: "Run the plan as shown", recommended: false },
       { label: "Don't run it", value: "no", description: "Cancel this task", recommended: false }]
    : optionsOf(q).map((o) => ({ ...o, value: o.label })));
  const picked = $derived(q ? choices.find((c) => c.value === answers[q.id]) : null);
  const own = $derived(q && answers[q.id] && !picked ? answers[q.id] : "");
  const done = $derived(questions.every((x) => (answers[x.id] || "").trim()));
  const steps = $derived((record?.steps || []).filter((s) => !["done", "skipped"].includes(s.status)));

  function choose(value) {
    answers[q.id] = answers[q.id] === value ? "" : value;
    if (answers[q.id]) next();
  }
  function next() {
    if (!q || !(answers[q.id] || "").trim()) return;
    if (questions.length === 1) return;
    at = Math.min(at + 1, questions.length);
  }
  function back() {
    at = Math.max(0, at - 1);
  }

  async function submit() {
    if (!done || busy || live) return;
    const approval = questions.find((x) => x.plan_fp);
    key ??= crypto.randomUUID();
    busy = true;
    const res = await act("tasks.answer", { task_id: task.id, answers: { ...answers }, plan_fp: approval?.plan_fp ?? null,
      idempotency_key: key });
    busy = false;
    if (res) {
      key = null;
      answers = {};
      try {
        localStorage.removeItem(storeKey);
      } catch {}
    }
  }

  function keys(e) {
    if (e.target.tagName === "TEXTAREA" || e.target.tagName === "INPUT") {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        questions.length === 1 || review ? submit() : next();
      }
      return;
    }
    const n = Number(e.key);
    if (n >= 1 && n <= choices.length) choose(choices[n - 1].value);
    else if (e.key === "Enter") (questions.length === 1 || review) ? submit() : next();
    else if (e.key === "ArrowLeft") back();
    else if (e.key === "ArrowRight") next();
  }
</script>

<!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
<div class="wizard" tabindex="0" onkeydown={keys} role="group" aria-label="Questions from Foreman">
  {#if questions.length > 1}
    <div class="tabs row" role="tablist">
      {#each questions as x, i}
        <button role="tab" aria-selected={i === at} class:on={i === at} class:answered={(answers[x.id] || "").trim()}
                onclick={() => (at = i)}>
          <span class="mark">{(answers[x.id] || "").trim() ? "✓" : i + 1}</span> {plain(x.header || x.kind)}
        </button>
      {/each}
      <button role="tab" aria-selected={review} class:on={review} disabled={!done} onclick={() => (at = questions.length)}>Review</button>
    </div>
  {/if}

  {#if q}
    <div class="ask">
      {#if q.kind === "approval" || q.kind === "dependency"}<span class="badge warn">{q.kind === "approval" ? "Approval" : "New dependency"}</span>{/if}
      <Markdown text={q.text} />
    </div>
    {#if q.kind === "approval" && steps.length}
      <div class="plan">
        <div class="muted small">The plan{record?.impact?.risk ? ` · ${record.impact.risk} risk` : ""}</div>
        <ol>{#each steps as s}<li><strong>{s.kind}</strong> in {s.project}{#if s.instructions}<span class="muted"> — {plain(s.instructions).slice(0, 160)}</span>{/if}</li>{/each}</ol>
      </div>
    {/if}
    <div class="choices" role="radiogroup" aria-label="Options">
      {#each choices as c, i}
        <button role="radio" aria-checked={picked === c} class:picked={picked === c} onclick={() => choose(c.value)} disabled={live}>
          <span class="num">{i + 1}</span>
          <span class="grow">
            <span class="label">{plain(c.label)}{#if c.recommended}&nbsp;<span class="rec">· recommended</span>{/if}</span>
            {#if c.description}<span class="desc muted">{plain(c.description)}</span>{/if}
          </span>
        </button>
      {/each}
      <label class="own" class:picked={!!own}>
        <span class="num">{choices.length + 1}</span>
        <input class="grow" value={own} disabled={live} placeholder={choices.length ? "Something else: type your answer" : "Type your answer"}
               oninput={(e) => (answers[q.id] = e.currentTarget.value)} aria-label="Your own answer" />
      </label>
    </div>
  {:else if review}
    <div class="review">
      {#each questions as x, i}
        <button class="line" onclick={() => (at = i)}>
          <span class="muted">{plain(x.header || x.kind)}</span>
          <span class="grow">{plain({ yes: x.kind === "approval" ? "Approve and run" : "yes", no: x.kind === "approval" ? "Don't run it" : "no" }[answers[x.id]] || answers[x.id])}</span>
          <span class="muted small">Change</span>
        </button>
      {/each}
    </div>
  {/if}

  <div class="foot row">
    {#if live}
      <span class="muted small grow">The task is still running; your answers are kept and can be sent when it stops.</span>
    {:else}
      <span class="muted small grow">{[questions.length > 1 && !review ? `Question ${at + 1} of ${questions.length}` : "",
        choices.length ? "Press a number to choose" : ""].filter(Boolean).join(" · ")}</span>
    {/if}
    {#if questions.length > 1 && at > 0}<button onclick={back}>Back</button>{/if}
    {#if questions.length > 1 && !review}
      <button class="primary" onclick={next} disabled={!q || !(answers[q.id] || "").trim()}>Next</button>
    {:else}
      <button class="primary" onclick={submit} disabled={!done || busy || live}>{busy ? "Sending…" : questions.length > 1 ? "Send answers" : "Send answer"}</button>
    {/if}
  </div>
</div>

<style>
  .wizard { border: 1px solid var(--accent); border-radius: 12px; padding: 14px 16px; display: flex; flex-direction: column;
    gap: 12px; background: var(--panel); outline: none; }
  .wizard:focus-visible { box-shadow: 0 0 0 2px var(--info-bg); }
  .tabs { gap: 4px; border-bottom: 1px solid var(--line); padding-bottom: 8px; }
  .tabs button { border: none; background: none; padding: 4px 10px; border-radius: 999px; font-size: 13px; color: var(--muted); }
  .tabs button.on { background: var(--sunken); color: var(--text); font-weight: 600; }
  .tabs .mark { display: inline-grid; place-items: center; width: 18px; height: 18px; border-radius: 50%; font-size: 11px;
    background: var(--sunken); margin-right: 2px; }
  .tabs button.answered .mark { background: var(--accent); color: var(--accent-text); }
  .ask { display: flex; flex-direction: column; gap: 6px; align-items: flex-start; font-size: 15px; }
  .plan { background: var(--sunken); border-radius: 8px; padding: 8px 12px; }
  .plan ol { margin: 4px 0 0; padding-left: 20px; font-size: 13.5px; }
  .choices { display: flex; flex-direction: column; gap: 6px; }
  .choices button, .own { display: flex; gap: 10px; align-items: center; text-align: left; padding: 9px 12px;
    border: 1px solid var(--line); border-radius: 8px; background: var(--panel); }
  .choices button:hover:not(:disabled) { border-color: var(--muted); }
  .choices .picked { border-color: var(--accent); background: var(--info-bg); }
  .num { width: 22px; height: 22px; border-radius: 6px; background: var(--sunken); display: grid; place-items: center;
    font-size: 12px; color: var(--muted); flex: none; }
  .picked .num { background: var(--accent); color: var(--accent-text); }
  .label { display: block; }
  .rec { color: var(--muted); font-size: 12.5px; }
  .desc { display: block; font-size: 12.5px; }
  .own input { border: none; background: transparent; padding: 0; }
  .own input:focus-visible { outline: none; }
  .own:focus-within { border-color: var(--accent); }
  .review { display: flex; flex-direction: column; gap: 4px; }
  .line { display: flex; gap: 12px; text-align: left; border: 1px solid var(--line); border-radius: 8px; padding: 8px 12px; }
  .line .muted:first-child { min-width: 120px; }
  .small { font-size: 12.5px; }
  .foot { gap: 8px; }
</style>
