fn main() {
    // The webview may call these commands and nothing else of the app's own (capabilities/default.json grants them).
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&["rpc_start", "rpc_send", "open_link", "pick_folder", "close_ack", "app_exit"]),
    ))
    .expect("failed to run the Tauri build script");
}
