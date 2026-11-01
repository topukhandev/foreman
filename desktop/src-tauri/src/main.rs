//! Foreman's desktop app: a window over `foreman serve --stdio` (docs/api-contract.md, docs/desktop-ui-plan.md).
//!
//! The webview gets six commands (build.rs, capabilities/default.json) and no plugins:
//! - `rpc_start`: start Foreman (again);
//! - `rpc_send`: one checked JSON-RPC request (policy::check_request). Answers to plan approvals and new
//!   dependencies are confirmed in a native dialog first, built from the task record (policy::confirmation);
//! - `open_link`: an http(s) link, opened in the browser after a native confirmation;
//! - `pick_folder`: the native folder picker (opening a project);
//! - `close_ack`, `app_exit`: the close flow (D3). Closing the window asks the page, which asks the user
//!   "keep running or stop all" when tasks run; if the page doesn't acknowledge within a few seconds, a second
//!   close closes anyway (tasks keep running: they are separate processes).

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod policy;
mod sidecar;

use serde_json::json;
use sidecar::Sidecar;
use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter, Manager, RunEvent, WindowEvent};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_opener::OpenerExt;

const CLOSE_GRACE: Duration = Duration::from_secs(3);

fn ask(app: &AppHandle, title: &str, text: String, yes: &str, no: &str, kind: MessageDialogKind) -> bool {
    app.dialog()
        .message(text)
        .title(title)
        .kind(kind)
        .buttons(MessageDialogButtons::OkCancelCustom(yes.into(), no.into()))
        .blocking_show()
}

#[tauri::command]
fn rpc_start(app: AppHandle) -> Result<(), String> {
    app.state::<Sidecar>().start(&app)
}

/// Runs off the main thread: a confirmation waits for the user.
fn send_checked(app: &AppHandle, line: &str) -> Result<(), String> {
    let msg = policy::check_request(line)?;
    #[cfg(debug_assertions)]
    eprintln!("[page] {}", msg["method"].as_str().unwrap_or("?"));
    let sidecar = app.state::<Sidecar>();
    if msg["method"] == "tasks.answer" {
        let params = &msg["params"];
        let answers = params["answers"].as_object().cloned().unwrap_or_default();
        let detail = sidecar.request("tasks.get", json!({"task_id": params["task_id"]}), Duration::from_secs(15))?;
        // A task Foreman can't load: the answer goes through and Foreman refuses it with the reason.
        if let Some(text) = policy::confirmation(&detail["result"]["record"], &answers) {
            if !ask(app, "Confirm your answer", text, "Send answer", "Don't send", MessageDialogKind::Warning) {
                let refusal = json!({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32000,
                    "message": "Not sent: you didn't confirm it.", "data": {"kind": "cancelled"}}});
                let _ = app.emit("rpc://message", refusal.to_string());
                return Ok(());
            }
        }
    }
    sidecar.send_line(&msg.to_string())
}

#[tauri::command]
async fn rpc_send(app: AppHandle, line: String) -> Result<(), String> {
    *app.state::<Sidecar>().close_asked.lock().unwrap() = None; // the page is alive
    tauri::async_runtime::spawn_blocking(move || send_checked(&app, &line)).await.map_err(|e| e.to_string())?
}

#[tauri::command]
async fn open_link(app: AppHandle, url: String) -> Result<(), String> {
    let url = policy::safe_link(&url).ok_or("Only http and https links open.")?;
    tauri::async_runtime::spawn_blocking(move || {
        let text = format!("Open this link in your browser?\n\n{}", policy::clean(&url, 400));
        if ask(&app, "Open link", text, "Open", "Cancel", MessageDialogKind::Info) {
            app.opener().open_url(url, None::<&str>).map_err(|e| e.to_string())
        } else {
            Ok(())
        }
    })
    .await
    .map_err(|e| e.to_string())?
}

/// The native folder picker (New conversation opens a project this way): the chosen folder, or None if cancelled.
#[tauri::command]
async fn pick_folder(app: AppHandle, start: Option<String>) -> Result<Option<String>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let mut dialog = app.dialog().file().set_title("Open a project folder");
        if let Some(dir) = start.filter(|d| std::path::Path::new(d).is_dir()) {
            dialog = dialog.set_directory(dir);
        }
        dialog.blocking_pick_folder().and_then(|f| f.into_path().ok()).map(|p| p.display().to_string())
    })
    .await
    .map_err(|e| e.to_string())
}

#[tauri::command]
fn close_ack(app: AppHandle) {
    *app.state::<Sidecar>().close_asked.lock().unwrap() = None;
}

#[tauri::command]
fn app_exit(app: AppHandle) {
    let sidecar = app.state::<Sidecar>();
    sidecar.exiting.store(true, Ordering::SeqCst);
    sidecar.shutdown();
    app.exit(0);
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(Sidecar::default())
        .invoke_handler(tauri::generate_handler![rpc_start, rpc_send, open_link, pick_folder, close_ack, app_exit])
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let sidecar = window.state::<Sidecar>();
                if sidecar.exiting.load(Ordering::SeqCst) {
                    return;
                }
                let mut asked = sidecar.close_asked.lock().unwrap();
                if asked.is_some_and(|t| t.elapsed() > CLOSE_GRACE) {
                    return; // the page never answered: close; tasks keep running on their own
                }
                asked.get_or_insert_with(Instant::now);
                api.prevent_close();
                let _ = window.emit("app://close-requested", ());
            }
        })
        .build(tauri::generate_context!())
        .expect("couldn't build the Foreman window");
    app.run(|app, event| {
        if let RunEvent::Exit = event {
            app.state::<Sidecar>().shutdown();
        }
    });
}
