//! Tauri commands - the bridge between webview and Rust.
//!
//! Each command is callable from the frontend via `tauri.invoke()`.
//! They typically proxy HTTP requests to the Python backend.

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, State};

use crate::sidecar::BackendHandle;

// ===== Chat =====

#[derive(Serialize, Deserialize, Debug)]
pub struct ChatRequest {
    pub query: String,
    pub session_id: Option<String>,
}

#[derive(Serialize, Deserialize, Debug)]
pub struct ChatResponse {
    pub answer: String,
    pub citations: Vec<Citation>,
    pub confidence: f32,
    pub refused: bool,
}

#[derive(Serialize, Deserialize, Debug)]
pub struct Citation {
    pub index: u32,
    pub file_name: String,
    pub page: Option<u32>,
    pub slide_number: Option<u32>,
    pub text: String,
}

/// Stream a chat completion (returns chunks via Tauri events).
#[tauri::command]
pub async fn chat_stream(
    request: ChatRequest,
    backend: State<'_, BackendHandle>,
) -> Result<String, String> {
    let client = reqwest::Client::new();
    let url = format!("{}/api/v1/chat", backend.url());

    let response = client
        .post(&url)
        .json(&request)
        .send()
        .await
        .map_err(|e| e.to_string())?;

    let result: ChatResponse = response
        .json()
        .await
        .map_err(|e| e.to_string())?;

    serde_json::to_string(&result).map_err(|e| e.to_string())
}

// ===== Knowledge Base =====

#[tauri::command]
pub async fn ingest_directory(
    path: String,
    recursive: bool,
    backend: State<'_, BackendHandle>,
) -> Result<String, String> {
    let client = reqwest::Client::new();
    let url = format!("{}/api/v1/ingest/dir", backend.url());

    let body = serde_json::json!({
        "path": path,
        "recursive": recursive,
        "force": false,
    });

    let response = client
        .post(&url)
        .json(&body)
        .send()
        .await
        .map_err(|e| e.to_string())?;

    response.text().await.map_err(|e| e.to_string())
}

#[derive(Serialize, Deserialize, Debug)]
pub struct KbFile {
    pub doc_id: String,
    pub file_name: String,
    pub file_path: String,
    pub file_type: String,
    pub chunk_count: u32,
}

#[tauri::command]
pub async fn list_kb_files(
    backend: State<'_, BackendHandle>,
) -> Result<Vec<KbFile>, String> {
    let client = reqwest::Client::new();
    let url = format!("{}/api/v1/kb/files", backend.url());

    let response = client.get(&url).send().await.map_err(|e| e.to_string())?;
    let body: serde_json::Value = response.json().await.map_err(|e| e.to_string())?;
    let files = body["files"].as_array().cloned().unwrap_or_default();

    let parsed: Vec<KbFile> = files
        .into_iter()
        .filter_map(|v| serde_json::from_value(v).ok())
        .collect();

    Ok(parsed)
}

#[tauri::command]
pub async fn delete_kb_file(
    doc_id: String,
    backend: State<'_, BackendHandle>,
) -> Result<(), String> {
    let client = reqwest::Client::new();
    let url = format!("{}/api/v1/kb/files/{}", backend.url(), doc_id);
    client.delete(&url).send().await.map_err(|e| e.to_string())?;
    Ok(())
}

// ===== Wiki =====

#[derive(Serialize, Deserialize, Debug)]
pub struct WikiPage {
    pub id: String,
    pub title: String,
    pub summary: String,
    pub category: String,
}

#[tauri::command]
pub async fn list_wiki_pages(
    backend: State<'_, BackendHandle>,
) -> Result<Vec<WikiPage>, String> {
    let client = reqwest::Client::new();
    let url = format!("{}/api/v1/wiki/pages", backend.url());

    let response = client.get(&url).send().await.map_err(|e| e.to_string())?;
    let body: serde_json::Value = response.json().await.map_err(|e| e.to_string())?;
    let pages = body["pages"].as_array().cloned().unwrap_or_default();

    let parsed: Vec<WikiPage> = pages
        .into_iter()
        .filter_map(|v| serde_json::from_value(v).ok())
        .collect();

    Ok(parsed)
}

#[tauri::command]
pub async fn get_wiki_page(
    page_id: String,
    backend: State<'_, BackendHandle>,
) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let url = format!("{}/api/v1/wiki/pages/{}", backend.url(), page_id);

    let response = client.get(&url).send().await.map_err(|e| e.to_string())?;
    response.json().await.map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn get_wiki_backlinks(
    page_id: String,
    backend: State<'_, BackendHandle>,
) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let url = format!(
        "{}/api/v1/wiki/pages/{}/backlinks",
        backend.url(),
        page_id
    );

    let response = client.get(&url).send().await.map_err(|e| e.to_string())?;
    response.json().await.map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn get_wiki_graph(
    backend: State<'_, BackendHandle>,
) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let url = format!("{}/api/v1/wiki/graph", backend.url());

    let response = client.get(&url).send().await.map_err(|e| e.to_string())?;
    response.json().await.map_err(|e| e.to_string())
}

// ===== Memory =====

#[tauri::command]
pub async fn search_episodic(
    topic: Option<String>,
    backend: State<'_, BackendHandle>,
) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let mut url = format!("{}/api/v1/memory/episodic", backend.url());
    if let Some(t) = topic {
        url.push_str(&format!("?topic={}", urlencoding::encode(&t)));
    }

    let response = client.get(&url).send().await.map_err(|e| e.to_string())?;
    response.json().await.map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn trigger_consolidation(
    backend: State<'_, BackendHandle>,
) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let url = format!("{}/api/v1/memory/consolidate", backend.url());

    let body = serde_json::json!({"reason": "manual", "force": false});

    let response = client
        .post(&url)
        .json(&body)
        .send()
        .await
        .map_err(|e| e.to_string())?;
    response.json().await.map_err(|e| e.to_string())
}

// ===== Native =====

/// Open native folder picker dialog.
#[tauri::command]
pub async fn pick_folder(app: AppHandle) -> Result<Option<String>, String> {
    use tauri_plugin_dialog::DialogExt;

    let (tx, rx) = std::sync::mpsc::channel();
    app.dialog()
        .file()
        .pick_folder(move |folder| {
            let _ = tx.send(folder);
        });

    let folder = rx.recv().map_err(|e| e.to_string())?;
    Ok(folder.map(|p| p.to_string()))
}

/// Kill the backend process (graceful shutdown).
#[tauri::command]
pub fn kill_backend(backend: State<'_, BackendHandle>) -> Result<(), String> {
    backend.kill();
    Ok(())
}

// ===== API key storage (encrypted via OS keyring) =====

#[tauri::command]
pub fn get_api_key(service: String, account: String) -> Result<Option<String>, String> {
    use crate::secrets;

    secrets::get_secret(&service, &account).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn set_api_key(
    service: String,
    account: String,
    value: String,
) -> Result<(), String> {
    use crate::secrets;

    secrets::set_secret(&service, &account, &value).map_err(|e| e.to_string())
}

// ===== App metadata =====

#[tauri::command]
pub fn app_info() -> serde_json::Value {
    serde_json::json!({
        "name": "YuanWork",
        "version": env!("CARGO_PKG_VERSION"),
        "platform": std::env::consts::OS,
    })
}
