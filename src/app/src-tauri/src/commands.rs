//! Tauri commands - the bridge between webview and Rust.
//!
//! Each command is callable from the frontend via `tauri.invoke()`.
//! They typically proxy HTTP requests to the Python backend.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, LazyLock, Mutex};
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
    /// The terms of the answer the reader may not know. Each one opens
    /// the text it stands for (ADR-041).
    #[serde(default)]
    pub terms: Vec<String>,
    #[serde(default)]
    pub trace: Vec<serde_json::Value>,
}

/// Where a citation points to.
///
/// The same shape the backend's anchor has: a kind, and the coordinates
/// that kind defines.
#[derive(Serialize, Deserialize, Debug, Clone, Default)]
pub struct CitationAnchor {
    /// "page" | "slide" | "section" | "sheet_row" | "heading_path"
    #[serde(default)]
    pub kind: String,
    #[serde(default)]
    pub page: Option<u32>,
    #[serde(default)]
    pub slide: Option<u32>,
    #[serde(default)]
    pub section: Option<String>,
    #[serde(default)]
    pub sheet: Option<String>,
    #[serde(default)]
    pub row: Option<u32>,
    #[serde(default)]
    pub heading_path: Vec<String>,
    /// Which text box in the document (Word keeps whole reports in
    /// floating text boxes).
    #[serde(default)]
    pub textbox_index: Option<u32>,
    /// Which picture in the document (scanned documents are pictures).
    #[serde(default)]
    pub image_index: Option<u32>,
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
    /// The worksheet of a spreadsheet, and the row inside it.
    #[serde(default)]
    pub sheet: Option<String>,
    #[serde(default)]
    pub row: Option<u32>,
    /// The position in the document, in the shape the shell takes.
    #[serde(default)]
    pub anchor: Option<CitationAnchor>,
    /// The position in words, e.g. "第 12 页".
    #[serde(default)]
    pub location: Option<String>,
    pub text: String,
    #[serde(default)]
    pub chunk_id: String,
}

/// Channel the backend's SSE events are re-emitted on, for the webview.
pub const CHAT_EVENT_CHANNEL: &str = "chat:event";

/// The flags of the streams that are running, by session.
///
/// The flag says "the user asked to stop". The reader of the stream
/// checks it between the chunks it receives.
static RUNNING_STREAMS: LazyLock<Mutex<HashMap<String, Arc<AtomicBool>>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

/// Stop a generation that is running.
///
/// The flag stops the reader here, and the backend is told as well - it
/// stops pulling from the model even if the connection is still open.
#[tauri::command]
pub async fn cancel_chat(
    session_id: String,
    backend: State<'_, BackendHandle>,
) -> Result<(), String> {
    let flag = RUNNING_STREAMS
        .lock()
        .ok()
        .and_then(|streams| streams.get(&session_id).cloned());
    if let Some(flag) = flag {
        flag.store(true, Ordering::SeqCst);
    }

    // The backend keeps its own registry - telling it is what makes it
    // stop pulling from the model.
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()
        .map_err(|e| e.to_string())?;
    let url = format!(
        "{}/api/v1/chat/cancel/{}",
        backend.url(),
        urlencoding::encode(&session_id)
    );
    match client.post(&url).send().await {
        Ok(response) if response.status().is_success() => Ok(()),
        Ok(response) => Err(format!("backend returned HTTP {}", response.status())),
        // The flag is already set - the reader stops either way.
        Err(_) => Ok(()),
    }
}

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

    // The flag says the user asked to stop this run.
    let session_id = request.session_id.clone().unwrap_or_default();
    let cancel_flag = Arc::new(AtomicBool::new(false));
    if !session_id.is_empty() {
        if let Ok(mut streams) = RUNNING_STREAMS.lock() {
            streams.insert(session_id.clone(), cancel_flag.clone());
        }
    }

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
    let mut cancelled = false;
    while let Some(chunk) = response.chunk().await.map_err(|e| e.to_string())? {
        // The user asked to stop: stop reading, and let the caller know
        // that the run did not finish.
        if cancel_flag.load(Ordering::SeqCst) {
            cancelled = true;
            break;
        }
        // A chunk boundary can fall inside a multi-byte character, so the
        // bytes are decoded incrementally - decoding each chunk on its own
        // would turn the split character into U+FFFD.
        let text = decoder.decode(&chunk);
        for event in parser.push(&text) {
            handle_stream_event(&app, &event, &mut final_response);
        }
    }
    if !session_id.is_empty() {
        if let Ok(mut streams) = RUNNING_STREAMS.lock() {
            streams.remove(&session_id);
        }
    }
    if cancelled {
        return Err("cancelled by the user".to_string());
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
    // A timeout here matters: the frontend polls this in a loop, and a
    // hung request would block the loop forever.
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .build()
        .map_err(|e| e.to_string())?;
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
    limit: Option<u32>,
    offset: Option<u32>,
) -> Result<Vec<KbFile>, String> {
    let client = reqwest::Client::new();
    // The list is paged: a knowledge base can hold thousands of files,
    // and the sidebar shows them a page at a time. The number of files
    // is not the length of this list - `kb_stats` is where the real
    // count comes from.
    let url = format!(
        "{}/api/v1/kb/files?limit={}&offset={}",
        backend.url(),
        limit.unwrap_or(100),
        offset.unwrap_or(0)
    );

    let response = client.get(&url).send().await.map_err(|e| e.to_string())?;
    // Without checking the status a backend error would come back as an
    // empty list - which the UI would read as "the knowledge base is
    // empty" rather than "something went wrong".
    let status = response.status();
    let body: serde_json::Value = response.json().await.map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(format!("HTTP {status}: {body}"));
    }
    let files = body["files"].as_array().cloned().unwrap_or_default();

    let parsed: Vec<KbFile> = files
        .into_iter()
        .filter_map(|v| serde_json::from_value(v).ok())
        .collect();

    Ok(parsed)
}

/// How much is in the knowledge base.
///
/// The file list is paged, so its length is not the number of files.
/// This is the number that says how much the build made of the
/// workspace.
#[tauri::command]
pub async fn kb_stats(
    backend: State<'_, BackendHandle>,
) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let url = format!("{}/api/v1/kb/stats", backend.url());

    let response = client.get(&url).send().await.map_err(|e| e.to_string())?;
    let status = response.status();
    let body: serde_json::Value = response.json().await.map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(format!("HTTP {status}: {body}"));
    }
    Ok(body)
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

// ===== Workspace =====

/// The folders and single files that make up the scope.
///
/// The workspace is what the build runs over: the user adds what they
/// want covered, and the build indexes what is in there. The list the
/// sidebar shows is this list - not the files that happen to be in the
/// index (AC-F-001).
#[tauri::command]
pub async fn list_workspace_entries(
    backend: State<'_, BackendHandle>,
) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let url = format!("{}/api/v1/workspace/entries", backend.url());

    let response = client.get(&url).send().await.map_err(|e| e.to_string())?;
    // Without checking the status a backend error would come back as an
    // empty list - which the UI would read as "the workspace is empty"
    // rather than "something went wrong".
    let status = response.status();
    let body: serde_json::Value = response.json().await.map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(format!("HTTP {status}: {body}"));
    }
    Ok(body)
}

/// Add a folder to the workspace.
///
/// The folder is not indexed here: it is covered by the build that runs
/// over the workspace. Adding it twice does not add it twice.
#[tauri::command]
pub async fn add_workspace_folder(
    path: String,
    backend: State<'_, BackendHandle>,
) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let url = format!("{}/api/v1/workspace/folders", backend.url());

    let body = serde_json::json!({ "path": path });

    let response = client
        .post(&url)
        .json(&body)
        .send()
        .await
        .map_err(|e| e.to_string())?;
    // The answer is the new entry - a rejected path must not be passed
    // off as one, so the status is checked first.
    let status = response.status();
    let body: serde_json::Value = response.json().await.map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(format!("HTTP {status}: {body}"));
    }
    Ok(body)
}

/// Add a single file to the workspace, by hand.
#[tauri::command]
pub async fn add_workspace_file(
    path: String,
    backend: State<'_, BackendHandle>,
) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let url = format!("{}/api/v1/workspace/files", backend.url());

    let body = serde_json::json!({ "path": path });

    let response = client
        .post(&url)
        .json(&body)
        .send()
        .await
        .map_err(|e| e.to_string())?;
    // The answer is the new entry - a path that is not a file must not
    // be passed off as one, so the status is checked first.
    let status = response.status();
    let body: serde_json::Value = response.json().await.map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(format!("HTTP {status}: {body}"));
    }
    Ok(body)
}

/// Take an entry out of the workspace.
///
/// The backend clears what that entry brought into the index with it -
/// the file itself stays where it is on disk.
#[tauri::command]
pub async fn remove_workspace_entry(
    entry_id: String,
    backend: State<'_, BackendHandle>,
) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let url = format!(
        "{}/api/v1/workspace/entries/{}",
        backend.url(),
        urlencoding::encode(&entry_id)
    );

    let response = client.delete(&url).send().await.map_err(|e| e.to_string())?;
    // Without checking the status a 404 would look like a success, and
    // the entry would stubbornly stay in the list.
    let status = response.status();
    let text = response.text().await.map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(format!("HTTP {status}: {text}"));
    }
    // The backend answers with what it cleared from the index - but a
    // removal that answers with nothing is still a removal.
    if text.trim().is_empty() {
        return Ok(serde_json::Value::Null);
    }
    serde_json::from_str(&text).map_err(|e| e.to_string())
}

/// Build the index over the workspace.
///
/// The backend kicks off a background job and answers with the job's
/// id; the progress is read from `/api/v1/ingest/status/{job_id}`, the
/// same way the folder build is watched.
#[tauri::command]
pub async fn rebuild_workspace(
    backend: State<'_, BackendHandle>,
) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let url = format!("{}/api/v1/ingest/rebuild", backend.url());

    let body = serde_json::json!({});

    let response = client
        .post(&url)
        .json(&body)
        .send()
        .await
        .map_err(|e| e.to_string())?;
    // The endpoint starts a background job, so a non-2xx status means
    // the request itself was rejected - there is no job to watch.
    let status = response.status();
    let body: serde_json::Value = response.json().await.map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(format!("HTTP {status}: {body}"));
    }
    Ok(body)
}

// ===== Wiki =====

#[derive(Serialize, Deserialize, Debug)]
pub struct WikiPage {
    pub id: String,
    pub title: String,
    pub summary: String,
    pub category: String,
    /// The tags of the page. The frontend filters the pages by them -
    /// a page without them used to crash the filter and take the whole
    /// window with it.
    #[serde(default)]
    pub tags: Vec<String>,
    /// The documents the page was compiled from.
    #[serde(default)]
    pub source_doc_ids: Vec<String>,
    /// How many pieces of evidence the page was compiled from.
    #[serde(default)]
    pub chunk_count: u32,
    /// How many times the page has been written.
    #[serde(default)]
    pub version: i64,
    /// The file the page was read from has changed since it was
    /// compiled - the page is kept, but it is the old version.
    #[serde(default)]
    pub stale: bool,
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

/// A conclusion on a page, and the place it was read from.
///
/// The anchor is what lets the reader jump back to the original: the
/// shell opens the file at that position.
#[derive(Serialize, Deserialize, Debug, Clone, Default)]
pub struct WikiConclusion {
    pub id: String,
    pub text: String,
    /// The position in the original document.
    #[serde(default)]
    pub anchor: Option<CitationAnchor>,
    /// The position in words, e.g. "第 12 页".
    #[serde(default)]
    pub location: String,
    /// The file the conclusion was read from.
    #[serde(default)]
    pub file_name: String,
    #[serde(default)]
    pub file_path: String,
}

/// The conclusions of a page, with their anchors.
#[tauri::command]
pub async fn list_page_conclusions(
    page_id: String,
    backend: State<'_, BackendHandle>,
) -> Result<Vec<WikiConclusion>, String> {
    let client = reqwest::Client::new();
    let url = format!(
        "{}/api/v1/wiki/pages/{}/conclusions",
        backend.url(),
        page_id
    );

    let response = client.get(&url).send().await.map_err(|e| e.to_string())?;
    let body: serde_json::Value = response.json().await.map_err(|e| e.to_string())?;
    let conclusions = body["conclusions"].as_array().cloned().unwrap_or_default();

    Ok(conclusions
        .into_iter()
        .filter_map(|v| serde_json::from_value(v).ok())
        .collect())
}

/// One paragraph of a page, and who wrote it.
///
/// The owner is what tells the reader whether a later automatic
/// recompile will take the paragraph away: the model's paragraphs are
/// replaced, the user's own are kept (AC-F-027).
#[derive(Serialize, Deserialize, Debug, Clone, Default)]
pub struct WikiPageBlock {
    /// The position of the paragraph in the page.
    #[serde(default)]
    pub index: u32,
    /// The paragraph itself.
    #[serde(default)]
    pub text: String,
    /// "model" (the model wrote it) or "user" (the user wrote it).
    #[serde(default)]
    pub owner: String,
}

/// The paragraphs of a page, in order, each with its owner.
#[tauri::command]
pub async fn list_page_blocks(
    page_id: String,
    backend: State<'_, BackendHandle>,
) -> Result<Vec<WikiPageBlock>, String> {
    let client = reqwest::Client::new();
    let url = format!("{}/api/v1/wiki/pages/{}/blocks", backend.url(), page_id);

    let response = client.get(&url).send().await.map_err(|e| e.to_string())?;
    // Without checking the status a page that does not exist would come
    // back as an empty list - and the page view would read that as "this
    // page has no paragraphs" instead of "this page is gone".
    let status = response.status();
    let body: serde_json::Value = response.json().await.map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(format!("HTTP {status}: {body}"));
    }
    let blocks = body["blocks"].as_array().cloned().unwrap_or_default();

    Ok(blocks
        .into_iter()
        .filter_map(|v| serde_json::from_value(v).ok())
        .collect())
}

/// Save the user's edit of a page body.
///
/// The backend decides which paragraphs belong to the user: the ones
/// that changed become the user's own, and a later automatic recompile
/// leaves them alone.
#[tauri::command]
pub async fn update_wiki_page(
    page_id: String,
    markdown: String,
    backend: State<'_, BackendHandle>,
) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let url = format!("{}/api/v1/wiki/pages/{}", backend.url(), page_id);
    let body = serde_json::json!({ "markdown": markdown });

    let response = client
        .put(&url)
        .json(&body)
        .send()
        .await
        .map_err(|e| e.to_string())?;
    // The answer is the updated page - a backend error must not be
    // passed off as one, so the status is checked first.
    let status = response.status();
    let body: serde_json::Value = response.json().await.map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(format!("HTTP {status}: {body}"));
    }
    Ok(body)
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

// ===== Terms =====

/// Where a term appears in the knowledge base.
///
/// The answer comes from the original documents, with the place it was
/// read from - not from a model. A term the knowledge base does not
/// hold comes back with `found: false` and no passages: the shell
/// passes that on as it is (ADR-041).
#[tauri::command]
pub async fn get_term_context(
    term: String,
    backend: State<'_, BackendHandle>,
) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let url = format!(
        "{}/api/v1/kb/terms/{}/context",
        backend.url(),
        urlencoding::encode(&term)
    );

    let response = client.get(&url).send().await.map_err(|e| e.to_string())?;
    let status = response.status();
    let body: serde_json::Value = response.json().await.map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(format!("HTTP {status}: {body}"));
    }
    Ok(body)
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

// ===== Opening the source at its position =====

/// What was opened, and where.
#[derive(Serialize, Deserialize, Debug)]
pub struct OpenedLocation {
    /// The file that was opened.
    pub path: String,
    /// The position in words - "第 12 页", "Slide 3", "进度表 第 7 行".
    pub location: String,
    /// How the position was reached: "office" or "none".
    pub method: String,
}

/// The command that opens a file.
#[derive(Debug, Clone, PartialEq)]
pub struct Launch {
    pub program: String,
    pub args: Vec<String>,
    /// "office" | "none"
    pub method: &'static str,
}

/// How a file is opened at a position.
///
/// Office has to be told through its own automation - that is the only
/// way to land on a slide or on a worksheet. Everything else opens in
/// the default program, and the app says where in the file the words
/// are (ADR-017).
pub fn plan_open(path: &str, anchor: &CitationAnchor) -> Launch {
    let extension = path.rsplit('.').next().unwrap_or("").to_ascii_lowercase();

    match extension.as_str() {
        // Office is told through its own automation - the only way to
        // land on a slide or on a worksheet.
        "ppt" | "pptx" => {
            if let Some(slide) = anchor.slide {
                return open_with_office(&powerpoint_script(path, slide));
            }
        }
        "xls" | "xlsx" => {
            if let Some(sheet) = anchor.sheet.as_ref().filter(|s| !s.trim().is_empty()) {
                return open_with_office(&excel_script(path, sheet, anchor.row));
            }
        }
        _ => {}
    }

    // Everything else - PDFs included - opens in the default program.
    //
    // It used to be `path#page=N` for PDFs, but that asks Windows for
    // a file *named* "…pdf#page=N" - the reader gets "file not found"
    // and the citation is dead. And the URL form (`file:///…`) is
    // worse: some readers (WPS) take it for something else entirely
    // and open the wrong document. The default program opens the file;
    // the app says which page the words are on.
    open_as_is(path)
}

/// The position in words - what the reader sees.
///
/// The words match the ones the backend uses, so the citation in the
/// answer and the notice after opening say the same thing.
pub fn describe_anchor(anchor: &CitationAnchor) -> String {
    match anchor.kind.as_str() {
        "page" => match anchor.page {
            Some(page) => format!("第 {page} 页"),
            None => String::new(),
        },
        "slide" => match anchor.slide {
            Some(slide) => format!("Slide {slide}"),
            None => String::new(),
        },
        "section" => {
            if let Some(section) = anchor.section.as_ref().filter(|s| !s.trim().is_empty()) {
                return section.clone();
            }
            if !anchor.heading_path.is_empty() {
                return anchor.heading_path.join(" › ");
            }
            // A Word document can hold text that is in neither a
            // paragraph nor a heading: it sits in a text box or in a
            // picture. The position is still a position - say which.
            if let Some(index) = anchor.image_index {
                return format!("第 {} 张图片", index + 1);
            }
            if let Some(index) = anchor.textbox_index {
                return format!("第 {} 个文本框", index + 1);
            }
            String::new()
        }
        "sheet_row" => match (&anchor.sheet, anchor.row) {
            (Some(sheet), Some(row)) => format!("{sheet} 第 {row} 行"),
            (Some(sheet), None) => sheet.clone(),
            (None, Some(row)) => format!("第 {row} 行"),
            _ => String::new(),
        },
        "heading_path" => anchor.heading_path.join(" › "),
        _ => String::new(),
    }
}

/// Open the file with the default program, as it is.
fn open_as_is(path: &str) -> Launch {
    Launch {
        program: "cmd".to_string(),
        // `start` needs the empty title, otherwise a quoted path is
        // taken for the window title.
        args: vec![
            "/C".to_string(),
            "start".to_string(),
            "".to_string(),
            path.to_string(),
        ],
        method: "none",
    }
}

/// Open the file through Office's own automation - the only way to
/// land on a slide or on a worksheet.
fn open_with_office(script: &str) -> Launch {
    Launch {
        program: "powershell".to_string(),
        args: vec![
            "-NoProfile".to_string(),
            "-WindowStyle".to_string(),
            "Hidden".to_string(),
            "-Command".to_string(),
            script.to_string(),
        ],
        method: "office",
    }
}

/// Open a presentation at a slide.
fn powerpoint_script(path: &str, slide: u32) -> String {
    format!(
        "$ErrorActionPreference='Stop'; \
         $app=New-Object -ComObject PowerPoint.Application; \
         $app.Visible=1; \
         $pres=$app.Presentations.Open('{path}',0,0,-1); \
         $pres.Slides.Item({slide}).Select();",
        path = escape_powershell(path),
        slide = slide,
    )
}

/// Open a spreadsheet at a worksheet, and scroll it to the row.
fn excel_script(path: &str, sheet: &str, row: Option<u32>) -> String {
    let mut script = format!(
        "$ErrorActionPreference='Stop'; \
         $app=New-Object -ComObject Excel.Application; \
         $app.Visible=$true; \
         $book=$app.Workbooks.Open('{path}'); \
         $worksheet=$book.Worksheets.Item('{sheet}'); \
         $worksheet.Activate();",
        path = escape_powershell(path),
        sheet = escape_powershell(sheet),
    );
    if let Some(row) = row {
        script.push_str(&format!(" $app.ActiveWindow.ScrollRow={row};"));
    }
    script
}

/// A single quote is the only character that would end the string
/// early.
fn escape_powershell(text: &str) -> String {
    text.replace('\'', "''")
}

/// Open the file a citation points at, at the position it points to.
///
/// The way to get there is different per format (ADR-017): Office is
/// told through its own automation, and everything else - PDFs
/// included - opens in the default program. The position comes back
/// in words ("第 12 页"), so the app can say where to look.
#[tauri::command]
pub fn open_at_anchor(
    path: String,
    anchor: Option<CitationAnchor>,
) -> Result<OpenedLocation, String> {
    if path.trim().is_empty() {
        return Err("这条引用没有文件路径".to_string());
    }
    if !std::path::Path::new(&path).exists() {
        return Err(format!("文件不存在：{path}"));
    }

    let anchor = anchor.unwrap_or_default();
    let launch = plan_open(&path, &anchor);

    // The process is not waited for: the file opens in another program,
    // and the office automation keeps running after the file is there.
    std::process::Command::new(&launch.program)
        .args(&launch.args)
        .spawn()
        .map_err(|e| format!("打不开这个文件：{e}"))?;

    Ok(OpenedLocation {
        path,
        location: describe_anchor(&anchor),
        method: launch.method.to_string(),
    })
}

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

#[cfg(test)]
mod tests {
    use super::*;

    fn anchor(kind: &str) -> CitationAnchor {
        CitationAnchor {
            kind: kind.to_string(),
            ..Default::default()
        }
    }

    #[test]
    fn open_at_anchor_opens_a_pdf_in_the_default_program() {
        // The page used to be glued onto the file name
        // ("…合同.pdf#page=12"), which asks Windows for a file *named*
        // that - the reader gets "file not found" and the citation is
        // dead. The default program opens the file; the app says which
        // page the words are on.
        let mut a = anchor("page");
        a.page = Some(12);

        let launch = plan_open("C:\\kb\\合同.pdf", &a);

        assert_eq!(launch.method, "none");
        assert!(
            launch.args.iter().any(|arg| arg == "C:\\kb\\合同.pdf"),
            "the file name is not in the launch command: {:?}",
            launch
        );
        assert!(
            !launch.args.iter().any(|arg| arg.contains("#page")),
            "the fragment must not be part of the file name: {:?}",
            launch
        );
    }

    #[test]
    fn the_page_of_a_citation_is_said_in_words() {
        let mut a = anchor("page");
        a.page = Some(12);
        assert_eq!(describe_anchor(&a), "第 12 页");
    }

    #[test]
    fn a_text_box_position_is_said_in_words() {
        // A Word report can keep its whole body in floating text
        // boxes - the position is still a position.
        let mut a = anchor("section");
        a.textbox_index = Some(1);
        assert_eq!(describe_anchor(&a), "第 2 个文本框");

        let mut b = anchor("section");
        b.image_index = Some(2);
        assert_eq!(describe_anchor(&b), "第 3 张图片");
    }

    #[test]
    fn open_at_anchor_opens_a_presentation_at_its_slide() {
        let mut a = anchor("slide");
        a.slide = Some(3);

        let launch = plan_open("C:\\kb\\方案.pptx", &a);

        assert_eq!(launch.method, "office");
        assert!(
            launch.args.iter().any(|arg| arg.contains("Slides.Item(3)")),
            "the slide is not in the launch command: {:?}",
            launch
        );
        assert!(
            launch.args.iter().any(|arg| arg.contains("方案.pptx")),
            "the file is not in the launch command: {:?}",
            launch
        );
    }

    #[test]
    fn open_at_anchor_opens_a_sheet_at_its_worksheet() {
        let mut a = anchor("sheet_row");
        a.sheet = Some("进度表".to_string());
        a.row = Some(7);

        let launch = plan_open("C:\\kb\\台账.xlsx", &a);

        assert_eq!(launch.method, "office");
        assert!(
            launch.args.iter().any(|arg| arg.contains("进度表")),
            "the worksheet is not in the launch command: {:?}",
            launch
        );
        assert!(
            launch.args.iter().any(|arg| arg.contains("ScrollRow=7")),
            "the row is not in the launch command: {:?}",
            launch
        );
    }

    #[test]
    fn open_at_anchor_opens_a_file_without_a_position_as_it_is() {
        let launch = plan_open("C:\\kb\\说明.md", &CitationAnchor::default());

        assert_eq!(launch.method, "none");
        assert!(launch.args.iter().any(|arg| arg == "C:\\kb\\说明.md"));
    }

    #[test]
    fn open_at_anchor_opens_a_pdf_without_a_page_as_it_is() {
        let launch = plan_open("C:\\kb\\合同.pdf", &CitationAnchor::default());

        assert_eq!(launch.method, "none");
    }

    #[test]
    fn open_at_anchor_describes_the_position_in_words() {
        let mut page = anchor("page");
        page.page = Some(12);
        assert_eq!(describe_anchor(&page), "第 12 页");

        let mut slide = anchor("slide");
        slide.slide = Some(3);
        assert_eq!(describe_anchor(&slide), "Slide 3");

        let mut row = anchor("sheet_row");
        row.sheet = Some("进度表".to_string());
        row.row = Some(7);
        assert_eq!(describe_anchor(&row), "进度表 第 7 行");

        assert_eq!(describe_anchor(&CitationAnchor::default()), "");
    }

    #[test]
    fn open_at_anchor_keeps_a_quote_in_the_path_from_ending_the_script() {
        let mut a = anchor("slide");
        a.slide = Some(1);

        let launch = plan_open("C:\\kb\\it's.pptx", &a);

        let script = launch.args.last().unwrap();
        assert!(
            script.contains("it''s.pptx"),
            "the quote was not escaped: {script}"
        );
    }
}
