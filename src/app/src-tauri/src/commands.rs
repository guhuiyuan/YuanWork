//! Tauri commands - the bridge between webview and Rust.
//!
//! Each command is callable from the frontend via `tauri.invoke()`.
//! They typically proxy HTTP requests to the Python backend.

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter, State};

use crate::sidecar::BackendHandle;
use crate::sse;

// ===== Chat =====

#[derive(Serialize, Deserialize, Debug)]
pub struct ChatRequest {
    pub query: String,
    pub session_id: Option<String>,
}

#[derive(Serialize, Deserialize, Debug, Default)]
pub struct ChatResponse {
    #[serde(default)]
    pub session_id: String,
    #[serde(default)]
    pub answer: String,
    #[serde(default)]
    pub citations: Vec<Citation>,
    #[serde(default)]
    pub confidence: f32,
    #[serde(default)]
    pub refused: bool,
    #[serde(default)]
    pub refusal_reason: Option<String>,
    #[serde(default)]
    pub conflicts: Vec<serde_json::Value>,
    #[serde(default)]
    pub wiki_compile: Option<serde_json::Value>,
    #[serde(default)]
    pub trace: Vec<serde_json::Value>,
}

#[derive(Serialize, Deserialize, Debug)]
pub struct Citation {
    pub index: u32,
    pub file_name: String,
    #[serde(default)]
    pub file_path: String,
    #[serde(default)]
    pub page: Option<u32>,
    #[serde(default)]
    pub slide_number: Option<u32>,
    #[serde(default)]
    pub section: Option<String>,
    pub text: String,
    #[serde(default)]
    pub chunk_id: String,
}

/// Channel the backend's SSE events are re-emitted on, for the webview.
pub const CHAT_EVENT_CHANNEL: &str = "chat:event";

/// How long the backend may go without sending any bytes before the
/// stream is considered stalled.
///
/// This is an *idle* timeout, not a total one: a long answer that keeps
/// trickling tokens is fine, only a silent connection is aborted.
pub const STREAM_IDLE_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(120);

/// Stream a chat completion.
///
/// Reads the SSE stream from the backend's `/api/v1/chat/stream` endpoint
/// and re-emits every event to the webview on the `chat:event` channel, so
/// the frontend can render the answer token by token. The command resolves
/// with the final response once the stream completes.
#[tauri::command]
pub async fn chat_stream(
    app: AppHandle,
    request: ChatRequest,
    backend: State<'_, BackendHandle>,
) -> Result<String, String> {
    let client = reqwest::Client::builder()
        // Idle timeout: resets on every byte received, so a slow but
        // progressing answer is not killed, only a stalled one is.
        .read_timeout(STREAM_IDLE_TIMEOUT)
        .connect_timeout(std::time::Duration::from_secs(30))
        .build()
        .map_err(|e| e.to_string())?;
    let url = format!("{}/api/v1/chat/stream", backend.url());

    let mut response = client
        .post(&url)
        .json(&request)
        .send()
        .await
        .map_err(|e| e.to_string())?;

    let status = response.status();
    if !status.is_success() {
        let body = response.text().await.unwrap_or_default();
        let message = format!("backend returned HTTP {status}: {body}");
        let _ = app.emit(
            CHAT_EVENT_CHANNEL,
            serde_json::json!({
                "type": "error",
                "data": {"error": "HTTP error", "detail": message},
            }),
        );
        return Err(message);
    }

    let mut parser = sse::SseParser::new();
    let mut decoder = sse::Utf8Decoder::new();
    let mut final_response: Option<ChatResponse> = None;

    // `Response::chunk()` needs no extra feature or dependency, unlike
    // `bytes_stream()` which would pull in futures-util.
    while let Some(chunk) = response.chunk().await.map_err(|e| e.to_string())? {
        // A chunk boundary can fall inside a multi-byte character, so the
        // bytes are decoded incrementally - decoding each chunk on its own
        // would turn the split character into U+FFFD.
        let text = decoder.decode(&chunk);
        for event in parser.push(&text) {
            handle_stream_event(&app, &event, &mut final_response);
        }
    }
    // The stream ended: flush bytes that never formed a character, then a
    // frame that was never terminated by a blank line.
    let tail = decoder.finish();
    for event in parser.push(&tail) {
        handle_stream_event(&app, &event, &mut final_response);
    }
    for event in parser.finish() {
        handle_stream_event(&app, &event, &mut final_response);
    }

    match final_response {
        Some(result) => serde_json::to_string(&result).map_err(|e| e.to_string()),
        None => Err("stream ended without a final response".to_string()),
    }
}

/// Forward one backend event to the webview, and pick up the final response.
fn handle_stream_event(
    app: &AppHandle,
    event: &sse::SseEvent,
    final_response: &mut Option<ChatResponse>,
) {
    let data: serde_json::Value = match serde_json::from_str(&event.data) {
        Ok(value) => value,
        // A non-JSON payload (e.g. the `ping` keepalive) is forwarded as-is.
        Err(_) => serde_json::Value::String(event.data.clone()),
    };

    if event.event == "done" {
        if let Some(response) = data.get("response") {
            if !response.is_null() {
                match serde_json::from_value::<ChatResponse>(response.clone()) {
                    Ok(parsed) => *final_response = Some(parsed),
                    Err(e) => {
                        let _ = app.emit(
                            CHAT_EVENT_CHANNEL,
                            serde_json::json!({
                                "type": "error",
                                "data": {
                                    "error": "malformed response",
                                    "detail": e.to_string(),
                                },
                            }),
                        );
                    }
                }
            }
        }
    }

    let _ = app.emit(
        CHAT_EVENT_CHANNEL,
        serde_json::json!({
            "type": event.event,
            "data": data,
        }),
    );
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

    // The endpoint kicks off a background job, so a non-2xx status means
    // the request itself was rejected (bad path, not a directory).
    let status = response.status();
    let text = response.text().await.map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(format!("HTTP {status}: {text}"));
    }
    Ok(text)
}

/// Poll the progress of an ingestion job.
///
/// Indexing runs in the background, so the file list only fills up once
/// the job reports "completed".
#[tauri::command]
pub async fn get_ingest_status(
    job_id: String,
    backend: State<'_, BackendHandle>,
) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let url = format!(
        "{}/api/v1/ingest/status/{}",
        backend.url(),
        urlencoding::encode(&job_id)
    );

    let response = client.get(&url).send().await.map_err(|e| e.to_string())?;
    let status = response.status();
    let body: serde_json::Value = response.json().await.map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(format!("HTTP {status}: {body}"));
    }
    Ok(body)
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
    let url = format!(
        "{}/api/v1/kb/files/{}",
        backend.url(),
        urlencoding::encode(&doc_id)
    );
    let response = client.delete(&url).send().await.map_err(|e| e.to_string())?;
    // Without checking the status a 404 would look like a success, and
    // the file would stubbornly stay in the list.
    let status = response.status();
    if !status.is_success() {
        let body = response.text().await.unwrap_or_default();
        return Err(format!("HTTP {status}: {body}"));
    }
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
