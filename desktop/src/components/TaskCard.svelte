<script>
  // A task inside its conversation. While it runs: what it is doing now and the latest activity. When it asks: the
  // questions, one at a time. When it is done: the result. Steps, checks and what the workers said stay under
  // "Details". Green only for results Foreman verified with tools.
  import { app, act, acceptSuggestion, dismissSuggestion, send } from "../lib/app.svelte.js";
  import { STATUS_TEXT, LEVEL_TEXT, CRITERION_BASIS, actions, isLive, stepCounts, tone } from "../lib/tasks.js";
  import { plain } from "../lib/render.js";
  import Markdown from "./Markdown.svelte";
  import QuestionWizard from "./QuestionWizard.svelte";

  let { task } = $props();

  const detail = $derived(app.details[task.id]);
  const record = $derived(detail?.record);
  const live = $derived(isLive(task));
  const can = $derived(actions(task));
  const phase = $derived(app.phases[task.id]);
  const steps = $derived(stepCounts(task));
  const level = $derived(task.verification?.level);
  const asking = $derived((task.questions || []).length > 0);
  const lines = $derived((app.activity[task.id] || []).filter((l) => l.event === "activity" || l.event === "step_start"
    || l.event === "check" || l.event === "step_done").slice(-4));
  const mine = $derived(app.suggestions.filter((s) => s.task === task.id));
  const answered = $derived((record?.questions || []).filter((q) => q.answer && !String(q.answer).startsWith("(")));
  let confirming = $state(null);
  let busy = $state(false);
  let note = $state("");

  async function run(method, params, message) {
    busy = true;
    await act(method, params, message);
    busy = false;
    confirming = null;
  }

  // The result, from the task's own fields (Foreman's text report is written for the terminal: its header, CLI hints
  // and question list would repeat what the card shows).
  const changedBy = $derived.by(() => {
    const out = {};
    for (const s of task.steps) if (s.kind === "implement" && s.status === "done" && s.summary && !out[s.project]) out[s.project] = s.summary;
    return Object.entries(out);
  });
  const stopped = $derived(task.status === "interrupted" ? "Stopped by you."
    : task.status === "paused" ? `Paused: ${record?.paused?.reason || "no provider is available right now"}.`
    : task.status === "blocked" ? "Blocked: a step couldn't go on." : task.status === "partial" ? "Only partly done." : "");
  const remaining = $derived((task.remaining || []).map((r) => (typeof r === "string" ? r : r.text || JSON.stringify(r))));
  const forYou = $derived((task.next_for_you || []).map((r) => (typeof r === "string" ? r : r.text || "")).filter(Boolean));
</script>

<article class="task" class:live>
  <div class="head row">
    {#if live}<span class="spin" aria-hidden="true"></span>
    {:else}<span class="badge {tone(task)}">{STATUS_TEXT[task.status] || task.status}</span>{/if}
    <strong class="grow title">{plain(task.title)}</strong>
    {#if level && !live}<span class="badge {level === 'tool_verified' ? 'ok' : level === 'failed' ? 'bad' : ''}" title="How far Foreman verified the result">{LEVEL_TEXT[level] || level}</span>{/if}
    <span class="muted small">#{task.id}{task.mode ? ` · ${task.mode}` : ""}</span>
  </div>

  {#if task.notices?.length}
    <ul class="notes" aria-label="Notes">{#each task.notices as n}<li>{plain(n)}</li>{/each}</ul>
  {/if}

  {#if live}
    <div class="now">
      <span>{phase ? plain(phase.text) : STATUS_TEXT[task.status] || "Working"}</span>
      {#if phase?.provider}<span class="muted"> · {phase.provider}</span>{/if}
      {#if steps.total}<span class="muted"> · step {Math.min(steps.done + 1, steps.total)} of {steps.total}</span>{/if}
    </div>
    {#if steps.total}<div class="bar" aria-hidden="true"><span style="width: {(100 * steps.done) / steps.total}%"></span></div>{/if}
    {#if lines.length}
      <ol class="tail">{#each lines as l}<li>{plain(l.text)}</li>{/each}</ol>
    {/if}
    <div class="row actions">
      <button class="link" onclick={() => (app.panel = { taskId: task.id, tab: "activity" })}>Show all activity</button>
      <span class="grow"></span>
      <button class="danger" disabled={busy} onclick={() => run("tasks.stop", { task_id: task.id }, "Stopping: the run ends at its next checkpoint.")}>Stop</button>
    </div>
  {:else}
    {#if stopped}<div class="stopped">{stopped}{record?.closed ? "" : " Continue picks it up where it stopped, in the same sessions."}</div>{/if}
    {#if asking}
      <QuestionWizard {task} {record} {live} />
    {:else}
      {#if task.answer}<div class="result"><Markdown text={task.answer} /></div>{/if}
      {#if changedBy.length}
        <div class="block"><div class="muted small">What changed <span>(as the workers describe it)</span></div>
          <ul>{#each changedBy as [project, text]}<li><strong>{project}</strong>: {plain(text)}</li>{/each}</ul></div>
      {/if}
      {#if record?.document}
        <div class="muted small">Saved <code>{record.document.project}/{record.document.path}</code></div>
      {/if}
      {#if remaining.length}
        <div class="block"><div class="muted small">Still open</div>
          <ul>{#each remaining.slice(0, 8) as r}<li>{plain(r)}</li>{/each}</ul></div>
      {/if}
      {#if task.next?.title && !live}
        <div class="row next">
          <button class="primary" onclick={() => send("next")}>Next: {plain(task.next.title)}</button>
          {#if task.next.reason}<span class="muted small grow">{plain(task.next.reason)}</span>{/if}
        </div>
      {/if}
      {#if forYou.length}
        <div class="block"><div class="muted small">You could ask next</div>
          <div class="chips">{#each forYou as f}<button onclick={() => (app.draft = f.replace(/^Ask me to /i, ""))}>{plain(f)}</button>{/each}</div></div>
      {/if}
    {/if}

    {#if mine.length && !asking}
      <div class="suggest">
        <div class="muted small">Worth considering</div>
        {#each mine as s (s.id)}
          <div class="row s">
            <span class="grow clamp" title={plain(s.why || "")}>{plain(s.text)}</span>
            <button onclick={() => acceptSuggestion(s)}>Do it</button>
            <button class="link" onclick={() => dismissSuggestion(s)}>Dismiss</button>
          </div>
        {/each}
      </div>
    {/if}

    {#if can.run || can.resume || can.close}
      <div class="row actions">
        {#if can.run}<button class="primary" disabled={busy} onclick={() => run("tasks.resume", { task_id: task.id })}>Run this plan</button>{/if}
        {#if can.resume && !can.run}
          <input class="grow" bind:value={note} placeholder="Continue: optionally say what to do differently" aria-label="Note" />
          <button class="primary" disabled={busy} onclick={() => run("tasks.resume", { task_id: task.id, note })}>Continue</button>
        {/if}
        {#if !can.resume || can.run}<span class="grow"></span>{/if}
        {#if can.close && !confirming}
          <button class="link" onclick={() => (confirming = "close")}>Mark finished</button>
          <button class="link" onclick={() => (confirming = "cancel")}>Cancel task</button>
        {/if}
      </div>
      {#if confirming}
        <div class="row confirm" role="alertdialog" aria-label="Confirm">
          <span class="grow">{confirming === "cancel" ? "Cancel this task? It won't be picked up again."
            : "Mark it as finished another way? Nothing in the projects changes."}</span>
          <button class="primary" disabled={busy} onclick={() => run("tasks.close", { task_id: task.id, cancel: confirming === "cancel" },
            confirming === "cancel" ? "Cancelled." : "Closed.")}>{confirming === "cancel" ? "Yes, cancel it" : "Yes, mark finished"}</button>
          <button onclick={() => (confirming = null)}>No</button>
        </div>
      {/if}
    {/if}
  {/if}

  {#if task.steps.length || task.checks.length || answered.length}
    <details>
      <summary class="muted small">Details: {steps.total ? `${steps.done}/${steps.total} steps` : ""}{task.checks.length ? ` · ${task.checks.filter((c) => c.ok).length}/${task.checks.length} checks passed` : ""}{answered.length ? ` · ${answered.length} answer${answered.length > 1 ? "s" : ""} given` : ""}</summary>
      <div class="more">
        {#if task.checks.length}
          <h3>Checks Foreman ran</h3>
          <ul>{#each task.checks as c}<li><span class={c.ok ? "pass" : "fail"}>{c.ok ? "✓" : "✗"}</span> <code>{c.command}</code> <span class="muted">in {c.project}</span></li>{/each}</ul>
        {/if}
        {#if task.verification?.gaps?.length}
          <h3>Not verified</h3>
          <ul>{#each task.verification.gaps as g}<li>{plain(g)}</li>{/each}</ul>
        {/if}
        {#if task.steps.length}
          <h3>Steps <span class="muted">(summaries are what the workers said)</span></h3>
          <ol>{#each task.steps as s}<li><span class="badge">{s.status}</span> <strong>{s.kind}</strong> {s.project}{#if s.provider}&nbsp;<span class="muted">via {s.provider}</span>{/if}
            {#if s.summary}<div class="says">{plain(s.summary)}</div>{/if}</li>{/each}</ol>
        {/if}
        {#if task.acceptance.length}
          <h3>Acceptance criteria</h3>
          <ul>{#each task.acceptance as c}<li><span class="badge {c.status === 'fail' ? 'bad' : ''}">{c.status || "not rated"}</span> {plain(c.text)} <span class="muted">({CRITERION_BASIS[c.level] || "not rated"})</span></li>{/each}</ul>
        {/if}
        {#if answered.length}
          <h3>Your answers</h3>
          <ul>{#each answered as q}<li><span class="muted">{plain(q.header || q.kind)}:</span> {plain(q.answer)}</li>{/each}</ul>
        {/if}
        <button class="link" onclick={() => (app.panel = { taskId: task.id, tab: "activity" })}>Activity and raw output</button>
      </div>
    </details>
  {/if}
</article>

<style>
  .task { border: 1px solid var(--line); border-radius: 12px; background: var(--panel); padding: 12px 16px;
    display: flex; flex-direction: column; gap: 10px; margin: 6px 0 14px; }
  .task.live { border-color: var(--accent); }
  .head { gap: 8px; flex-wrap: nowrap; }
  .title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .small { font-size: 12.5px; }
  .now { font-size: 14px; }
  .bar { height: 4px; background: var(--sunken); border-radius: 999px; overflow: hidden; }
  .bar span { display: block; height: 100%; background: var(--accent); transition: width .3s; }
  .tail { list-style: none; margin: 0; padding: 8px 10px; background: var(--sunken); border-radius: 8px;
    font-family: var(--mono); font-size: 12px; color: var(--muted); display: flex; flex-direction: column; gap: 2px; }
  .tail li { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tail li:last-child { color: var(--text); }
  .result { line-height: 1.55; }
  .notes { margin: 0; padding: 6px 10px 6px 26px; background: var(--warn-bg); color: var(--warn); border-radius: 8px;
    font-size: 13px; }
  .notes li + li { margin-top: 2px; }
  .stopped { background: var(--warn-bg); color: var(--warn); border-radius: 8px; padding: 6px 10px; font-size: 13.5px; }
  .block ul { margin: 4px 0 0; padding-left: 20px; }
  .block li + li { margin-top: 3px; }
  .next { gap: 10px; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
  .chips button { border-radius: 999px; font-size: 13px; text-align: left; color: var(--muted); max-width: 100%; }
  .chips button:hover { color: var(--text); }
  .result :global(h1), .result :global(h2) { font-size: 15px; margin: 12px 0 6px; }
  .result :global(h3) { font-size: 13.5px; text-transform: none; letter-spacing: 0; color: var(--text); margin: 10px 0 4px; }
  .actions { gap: 8px; }
  .link { border: none; background: none; padding: 2px 4px; color: var(--muted); font-size: 13px; }
  .link:hover { color: var(--text); text-decoration: underline; }
  .confirm { background: var(--warn-bg); padding: 8px 10px; border-radius: 8px; }
  .suggest { border-top: 1px dashed var(--line); padding-top: 8px; display: flex; flex-direction: column; gap: 6px; }
  .s { gap: 8px; font-size: 13.5px; flex-wrap: nowrap; }
  .clamp { display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  details summary { cursor: pointer; list-style: revert; }
  .more { padding: 6px 0 2px; font-size: 13.5px; }
  .more h3 { margin: 10px 0 4px; font-size: 12px; }
  .more ul, .more ol { margin: 0; padding-left: 20px; }
  .more li + li { margin-top: 4px; }
  .says { color: var(--muted); margin-top: 2px; overflow-wrap: anywhere; }
  .pass { color: var(--ok); font-weight: 600; }
  .fail { color: var(--bad); font-weight: 600; }
  .spin { width: 12px; height: 12px; border-radius: 50%; border: 2px solid var(--accent); border-right-color: transparent;
    animation: spin .9s linear infinite; flex: none; }
  @keyframes spin { to { transform: rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
</style>
