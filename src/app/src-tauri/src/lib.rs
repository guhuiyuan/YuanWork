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

/// Show a fatal error to the user in a native message box.
///
/// The window is not up yet when this happens, so the webview cannot
/// say it - and an app that silently refuses to open is the worst
/// kind of failure. Windows only: the message box is the only thing
/// that is guaranteed to be there before the app is running.
#[cfg(target_os = "windows")]
fn show_fatal_error(title: &str, message: &str) {
    use std::ffi::c_void;

    #[link(name = "user32")]
    extern "system" {
        fn MessageBoxW(
            hwnd: *mut c_void,
            text: *const u16,
            caption: *const u16,
            kind: u32,
        ) -> i32;
    }

    const MB_OK: u32 = 0x0000_0000;
    const MB_ICONERROR: u32 = 0x0000_0010;

    let text: Vec<u16> = message.encode_utf16().chain(std::iter::once(0)).collect();
    let caption: Vec<u16> = title.encode_utf16().chain(std::iter::once(0)).collect();
    unsafe {
        MessageBoxW(
            std::ptr::null_mut(),
            text.as_ptr(),
            caption.as_ptr(),
            MB_OK | MB_ICONERROR,
        );
    }
}

#[cfg(not(target_os = "windows"))]
fn show_fatal_error(title: &str, message: &str) {
    log::error!("{}: {}", title, message);
}

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
            let backend_handle = match sidecar::start_backend(app.handle()) {
                Ok(handle) => handle,
                Err(e) => {
                    // The app cannot work without its backend, and the
                    // window would just show an empty shell. Say what
                    // went wrong - a port held by a leftover backend is
                    // exactly the kind of thing the user cannot guess.
                    log::error!("Backend did not start: {}", e);
                    if let Some(window) = app.get_webview_window("main") {
                        let _ = window.hide();
                    }
                    show_fatal_error("YuanWork 后端没有启动", &e.to_string());
                    return Err(e);
                }
            };
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
            commands::kb_stats,
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
