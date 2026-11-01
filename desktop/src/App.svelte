<script>
  // Layout like a chat app: projects and their conversations on the left, the open conversation in the middle with
  // the message box at the bottom, and (when asked for) a task's full activity and output on the right.
  import { onMount } from "svelte";
  import { app, connect, openConversation, openProject, openTask } from "./lib/app.svelte.js";
  import { onCloseRequested } from "./lib/shell.js";
  import Sidebar from "./components/Sidebar.svelte";
  import Conversation from "./components/Conversation.svelte";
  import SidePanel from "./components/SidePanel.svelte";
  import SettingsDialog from "./components/SettingsDialog.svelte";
  import CloseDialog from "./components/CloseDialog.svelte";

  let sidebarOpen = $state(true);

  onMount(() => {
    connect().then(() => {
      // a place named in the address, for screenshots of the demo: #task/3, #conversation/<project>/<id>, #settings
      const h = location.hash;
      let m;
      if ((m = /^#task\/(\d+)$/.exec(h))) openTask(m[1]);
      else if ((m = /^#conversation\/([^/]+)\/(.+)$/.exec(h))) openConversation(decodeURIComponent(m[1]), m[2]);
      else if ((m = /^#settings(?:\/(\w+))?$/.exec(h))) app.settings = m[1] || "general";
    });
    let unlisten = () => {};
    onCloseRequested(() => (app.closing = true)).then((u) => (unlisten = u));
    return () => unlisten();
  });

  function typing(target) {
    return target instanceof HTMLElement && (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));
  }

  // Ctrl/Cmd+K or "/" to the message box, Ctrl/Cmd+N for a new conversation, Ctrl/Cmd+, for settings,
  // Ctrl/Cmd+B for the sidebar, Escape closes the settings or the side panel.
  function keys(e) {
    const mod = e.ctrlKey || e.metaKey;
    const k = e.key.toLowerCase();
    if (mod && k === "k") {
      e.preventDefault();
      document.getElementById("composer")?.focus();
    } else if (mod && k === "n") {
      e.preventDefault();
      openProject();
    } else if (mod && e.key === ",") {
      e.preventDefault();
      app.settings = app.settings ? null : "general";
    } else if (mod && k === "b") {
      e.preventDefault();
      sidebarOpen = !sidebarOpen;
    } else if (e.key === "Escape" && !app.closing) {
      if (app.settings) app.settings = null;
      else if (app.panel && !typing(e.target)) app.panel = null;
    } else if (e.key === "/" && !typing(e.target) && !app.settings) {
      e.preventDefault();
      document.getElementById("composer")?.focus();
    }
  }
</script>

<svelte:window onkeydown={keys} />

<div class="shell" class:collapsed={!sidebarOpen} class:panel={!!app.panel}>
  {#if sidebarOpen}<Sidebar onCollapse={() => (sidebarOpen = false)} />{/if}

  <main>
    {#if !sidebarOpen}
      <button class="reopen" onclick={() => (sidebarOpen = true)} title="Show the sidebar (Ctrl+B)" aria-label="Show the sidebar">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v14H4zM9 5v14" /></svg>
      </button>
    {/if}
    {#if app.demo}
      <div class="banner info">Demo data: the page in a browser, not connected to Foreman. <code>npm run tauri dev</code> runs the app.</div>
    {/if}
    {#if app.failure && !app.connecting}
      <div class="banner bad" role="alert">
        <div class="row">
          <strong class="grow">Foreman isn't running: {app.failure.message}</strong>
          <button onclick={connect}>Start again</button>
        </div>
        {#if app.failure.log}<pre class="log">{app.failure.log}</pre>{/if}
      </div>
    {/if}
    <Conversation />
  </main>

  {#if app.panel}<SidePanel />{/if}
</div>

{#if app.notice}
  <div class="notice" role="status">
    <span class="grow">{app.notice}</span>
    <button class="x" onclick={() => (app.notice = "")} aria-label="Dismiss">×</button>
  </div>
{/if}

{#if app.settings}<SettingsDialog />{/if}
{#if app.closing}<CloseDialog />{/if}

<style>
  .shell { display: grid; grid-template-columns: 264px minmax(0, 1fr); height: 100vh; }
  .shell.panel { grid-template-columns: 264px minmax(0, 1fr) minmax(320px, 34%); }
  .shell.collapsed { grid-template-columns: minmax(0, 1fr); }
  .shell.collapsed.panel { grid-template-columns: minmax(0, 1fr) minmax(320px, 34%); }
  main { display: flex; flex-direction: column; min-width: 0; min-height: 0; position: relative; }
  .reopen { position: absolute; top: 12px; left: 12px; z-index: 2; border: none; background: none; padding: 4px; color: var(--muted); }
  .reopen svg { width: 20px; height: 20px; fill: none; stroke: currentColor; stroke-width: 1.8; }
  .banner { margin: 12px 24px 0; padding: 10px 14px; border-radius: var(--radius); }
  .banner.info { background: var(--info-bg); }
  .banner.bad { background: var(--bad-bg); color: var(--bad); }
  .log { max-height: 160px; overflow: auto; white-space: pre-wrap; color: var(--text); margin: 8px 0 0; }
  .notice { position: fixed; left: 50%; bottom: 112px; transform: translateX(-50%); z-index: 20; display: flex; gap: 10px;
    align-items: center; max-width: min(640px, calc(100vw - 32px)); padding: 9px 12px 9px 16px; background: var(--text);
    color: var(--bg); border-radius: 10px; box-shadow: 0 6px 24px rgb(0 0 0 / .18); }
  .x { border: none; background: none; color: inherit; font-size: 16px; padding: 0 4px; }
  @media (max-width: 760px) {
    .shell, .shell.panel { grid-template-columns: minmax(0, 1fr); }
    .shell :global(aside.sidebar) { position: fixed; inset: 0 auto 0 0; width: min(300px, 86vw); z-index: 30; box-shadow: 0 0 40px rgb(0 0 0 / .3); }
    .shell :global(aside.panel) { position: fixed; inset: 0 0 0 auto; width: min(420px, 92vw); z-index: 30; }
  }
</style>
