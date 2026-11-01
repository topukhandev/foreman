// The few things the app asks of the Tauri shell besides the sidecar (src-tauri/src/main.rs): open a link (the shell
// asks first and allows http(s) only) and quit. In the browser demo they do nothing harmful.

import { inTauri } from "./rpc.js";
import { safeUrl } from "./render.js";

async function invoke(cmd, args) {
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke(cmd, args);
}

export async function openLink(url) {
  const safe = safeUrl(url);
  if (!safe) return;
  if (inTauri()) await invoke("open_link", { url: safe });
  else window.open(safe, "_blank", "noopener,noreferrer");
}

/** The native folder picker; in the browser demo, a prompt. The chosen folder, or null. */
export async function pickFolder(start) {
  if (inTauri()) return invoke("pick_folder", { start: start || null });
  const typed = window.prompt("Folder to open (the browser demo can't show the folder picker):", start || "");
  return typed && typed.trim() ? typed.trim() : null;
}

export async function exitApp() {
  if (inTauri()) await invoke("app_exit");
}

export async function onCloseRequested(callback) {
  if (!inTauri()) return () => {};
  const { listen } = await import("@tauri-apps/api/event");
  return listen("app://close-requested", () => {
    invoke("close_ack"); // the page is alive: the shell waits for its answer instead of closing anyway
    callback();
  });
}
