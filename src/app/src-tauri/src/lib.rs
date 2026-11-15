//! YuanWork library entry - exposes the `run` function for the Tauri app.
//!
//! This module orchestrates the Tauri shell, including:
//! - Starting the Python backend as a sidecar
//! - Exposing IPC commands to the webview
//! - Managing the backend process lifecycle

mod sidecar;
mod commands;
mod commands_contract;
mod fs_scope;
mod secrets;
mod sse;

use tauri::Manager;

/// Application entry point.
/// Called from `main.rs`.
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    env_logger::init();

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            // Start the Python backend as a sidecar
            let backend_handle = sidecar::start_backend(app.handle())?;
            app.manage(backend_handle);

            // Open main window
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
            }

            Ok(())
        })
        .on_window_event(|window, event| {
            // On window close, kill the backend
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                if let Some(handle) = window.app_handle().try_state::<sidecar::BackendHandle>() {
                    handle.kill();
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            commands::chat_stream,
            commands::ingest_directory,
            commands::get_ingest_status,
            commands::pick_folder,
            commands::kill_backend,
            commands::get_api_key,
            commands::set_api_key,
            commands::list_kb_files,
            commands::delete_kb_file,
            commands::list_wiki_pages,
            commands::get_wiki_page,
            commands::get_wiki_backlinks,
            commands::list_page_conclusions,
            commands::list_page_blocks,
            commands::update_wiki_page,
            commands::get_term_context,
            commands::get_wiki_graph,
            commands::search_episodic,
            commands::trigger_consolidation,
            commands::app_info,
            commands::open_at_anchor,
            commands::cancel_chat,
            commands::list_workspace_entries,
            commands::add_workspace_folder,
            commands::add_workspace_file,
            commands::remove_workspace_entry,
            commands::rebuild_workspace,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
