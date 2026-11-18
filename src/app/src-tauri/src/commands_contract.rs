//! Contract tests for the Tauri command surface.
//!
//! The command list is the contract between the webview and the shell:
//! renaming one silently breaks the frontend, because the `invoke`
//! call then fails at runtime with "command not found" — and nothing in
//! the type system notices.

use crate::commands::{ChatResponse, KbFile, WikiConclusion, WikiPage, WikiPageBlock};
use serde_json::json;

/// The commands the webview may invoke.
///
/// Keep in sync with `generate_handler!` in `lib.rs`.
const COMMAND_NAMES: [&str; 28] = [
    "chat_stream",
    "ingest_directory",
    "get_ingest_status",
    "pick_folder",
    "kill_backend",
    "get_api_key",
    "set_api_key",
    "list_kb_files",
    "kb_stats",
    "delete_kb_file",
    "list_wiki_pages",
    "get_wiki_page",
    "get_wiki_backlinks",
    "list_page_conclusions",
    "list_page_blocks",
    "update_wiki_page",
    "get_term_context",
    "get_wiki_graph",
    "search_episodic",
    "trigger_consolidation",
    "app_info",
    "open_at_anchor",
    "cancel_chat",
    "list_workspace_entries",
    "add_workspace_folder",
    "add_workspace_file",
    "remove_workspace_entry",
    "rebuild_workspace",
];

/// The commands that check the backend's HTTP status before treating the
/// body as a result. The other ones take the body as it comes, so a
/// backend error surfaces as "no data" instead of an error.
const STATUS_CHECKING_COMMANDS: [&str; 12] = [
    "list_kb_files",
    "kb_stats",
    "delete_kb_file",
    "ingest_directory",
    "list_page_blocks",
    "update_wiki_page",
    "get_term_context",
    "list_workspace_entries",
    "add_workspace_folder",
    "add_workspace_file",
    "remove_workspace_entry",
    "rebuild_workspace",
];

#[test]
fn every_command_is_registered() {
    let lib = include_str!("../src/lib.rs");
    for name in COMMAND_NAMES {
        assert!(
            lib.contains(name),
            "command `{}` is not registered in lib.rs",
            name
        );
    }
}

#[test]
fn the_command_list_has_not_grown_or_shrunk() {
    let lib = include_str!("../src/lib.rs");
    let registered = lib
        .lines()
        .filter(|line| line.trim_start().starts_with("commands::"))
        .count();
    assert_eq!(
        registered,
        COMMAND_NAMES.len(),
        "the number of registered commands changed"
    );
}

/// The body of a command, as it is written in commands.rs.
fn command_body(commands: &str, name: &str) -> String {
    let start = commands
        .find(&format!("pub async fn {}", name))
        .unwrap_or_else(|| panic!("command `{}` is missing from commands.rs", name));
    let rest = &commands[start..];
    let end = rest
        .find("\n}\n")
        .unwrap_or_else(|| panic!("the body of `{}` has no end", name));
    rest[..end].to_string()
}

#[test]
fn the_status_checking_commands_report_the_status_code() {
    let commands = include_str!("../src/commands.rs");
    for name in STATUS_CHECKING_COMMANDS {
        assert!(
            commands.contains(name),
            "command `{}` is missing from commands.rs",
            name
        );
        // The name being there is not enough - the command has to
        // check the status before it takes the body as a result. A
        // missing page would otherwise come back as an empty list, and
        // the app would read that as "this page has no paragraphs".
        let body = command_body(commands, name);
        assert!(
            body.contains("is_success()"),
            "command `{}` does not check the status of the response",
            name
        );
    }
    // the commands that read the body after checking the status
    assert!(commands.contains(r#"Err(format!("HTTP {status}: {body}"))"#),
        "the HTTP error shape changed"
    );
}

#[test]
fn the_commands_return_the_error_as_a_bare_string() {
    let commands = include_str!("../src/commands.rs");
    // every fallible command maps its error into a String
    assert!(commands.contains("map_err(|e| e.to_string())?"));
    assert!(commands.contains(r#"Err(format!("HTTP {status}: {text}"))"#));
}

// ===== the wiki page fields =====

#[test]
fn the_wiki_page_carries_the_fields_the_frontend_uses() {
    let backend_page = json!({
        "id": "p1",
        "title": "标题",
        "summary": "摘要",
        "category": "contract",
        "tags": ["a", "b"],
        "source_doc_ids": ["d1"],
        "chunk_count": 12,
        "version": 3,
        "created_at": "2026-01-01",
        "updated_at": "2026-01-02",
        "last_compiled_at": "2026-01-03",
    });

    let page: WikiPage = serde_json::from_value(backend_page).expect("should parse");
    assert_eq!(page.id, "p1");
    assert_eq!(page.title, "标题");
    assert_eq!(page.summary, "摘要");
    assert_eq!(page.category, "contract");
    // The frontend filters the pages by their tags - a page without them
    // crashed the filter and took the whole window with it.
    assert_eq!(page.tags, vec!["a".to_string(), "b".to_string()]);
    assert_eq!(page.source_doc_ids, vec!["d1".to_string()]);
    assert_eq!(page.chunk_count, 12);
    assert_eq!(page.version, 3);
}

#[test]
fn the_bridge_sends_the_tags_the_frontend_filters_by() {
    let backend_page = json!({
        "id": "p1",
        "title": "标题",
        "summary": "摘要",
        "category": "contract",
        "tags": ["违约"],
        "source_doc_ids": ["d1"],
        "chunk_count": 12,
        "version": 3,
    });

    let bridge: WikiPage = serde_json::from_value(backend_page).expect("should parse");
    let serialised = serde_json::to_value(&bridge).unwrap();

    assert_eq!(serialised.get("tags"), Some(&json!(["违约"])));
    assert_eq!(serialised.get("chunk_count"), Some(&json!(12)));
}

#[test]
fn a_wiki_page_without_the_extra_fields_still_parses() {
    /// The bridge must not depend on the backend sending every field -
    /// an older backend sends fewer.
    let bridge: WikiPage = serde_json::from_value(json!({
        "id": "p1", "title": "标题", "summary": "摘要", "category": "contract",
    }))
    .expect("should parse");

    let serialised = serde_json::to_value(&bridge).unwrap();
    assert_eq!(serialised.get("tags"), Some(&json!([])));
    assert_eq!(serialised.get("chunk_count"), Some(&json!(0)));
}

#[test]
fn the_wiki_page_carries_the_stale_marker() {
    /// The frontend says "the source file has changed" next to the page
    /// - the bridge must not drop the flag.
    let backend_page = json!({
        "id": "p1",
        "title": "标题",
        "summary": "摘要",
        "category": "contract",
        "stale": true,
    });

    let bridge: WikiPage = serde_json::from_value(backend_page).expect("should parse");
    assert!(bridge.stale);

    let serialised = serde_json::to_value(&bridge).unwrap();
    assert_eq!(serialised.get("stale"), Some(&json!(true)));
}

#[test]
fn the_conclusion_carries_the_place_it_was_read_from() {
    /// A conclusion is only worth reading if the reader can check it -
    /// the anchor and the file are what make that possible.
    let backend_conclusion = json!({
        "id": "c1",
        "text": "验收期限为 30 日",
        "anchor": {"kind": "page", "page": 12},
        "location": "第 12 页",
        "file_name": "采购合同.pdf",
        "file_path": "C:/kb/采购合同.pdf",
    });

    let conclusion: WikiConclusion =
        serde_json::from_value(backend_conclusion).expect("should parse");

    assert_eq!(conclusion.id, "c1");
    assert_eq!(conclusion.text, "验收期限为 30 日");
    assert_eq!(conclusion.location, "第 12 页");
    assert_eq!(conclusion.file_name, "采购合同.pdf");
    assert_eq!(conclusion.file_path, "C:/kb/采购合同.pdf");

    let anchor = conclusion.anchor.expect("the anchor was dropped");
    assert_eq!(anchor.kind, "page");
    assert_eq!(anchor.page, Some(12));
}

#[test]
fn a_conclusion_without_an_anchor_still_parses() {
    /// The bridge must not depend on the backend sending every field -
    /// a conclusion without a place is still a conclusion.
    let bridge: WikiConclusion = serde_json::from_value(json!({
        "id": "c1", "text": "验收期限为 30 日",
    }))
    .expect("should parse");

    assert!(bridge.anchor.is_none());
    assert_eq!(bridge.location, "");
}

#[test]
fn the_knowledge_base_file_keeps_five_fields() {
    let backend_file = json!({
        "doc_id": "d1",
        "file_name": "a.pdf",
        "file_path": "/kb/a.pdf",
        "file_type": "pdf",
        "file_hash": "h",
        "chunk_count": 3,
        "size_bytes": 100,
        "indexed_at": "2026-01-01",
        "last_modified": "2026-01-01",
    });

    let file: KbFile = serde_json::from_value(backend_file).expect("should parse");
    assert_eq!(file.doc_id, "d1");
    assert_eq!(file.chunk_count, 3);

    let serialised = serde_json::to_value(&file).unwrap();
    assert!(serialised.get("file_hash").is_none());
    assert!(serialised.get("size_bytes").is_none());
}

// ===== the terms of an answer =====

#[test]
fn the_chat_response_carries_the_terms_of_the_answer() {
    /// The terms are the words the reader may not know - each one
    /// opens the text it stands for (ADR-041). The bridge must not drop
    /// them: the desktop path resolves the response from this struct.
    let response: ChatResponse = serde_json::from_value(json!({
        "answer": "履约保证金按合同金额的百分之五缴纳。",
        "terms": ["履约保证金"],
    }))
    .expect("should parse");

    assert_eq!(response.terms, vec!["履约保证金".to_string()]);
}

#[test]
fn a_response_without_terms_still_parses() {
    /// An older backend sends no terms at all - the answer is still an
    /// answer.
    let response: ChatResponse = serde_json::from_value(json!({
        "answer": "答案 [1]。",
    }))
    .expect("should parse");

    assert!(response.terms.is_empty());
}

// ===== the paragraphs of a page =====
#[test]
fn the_page_block_carries_the_fields_the_page_view_uses() {
    /// The page view renders the body from the blocks, and says who
    /// wrote each paragraph - the bridge must not drop either.
    let backend_block = json!({
        "index": 0,
        "text": "验收期限为 30 日",
        "owner": "user",
    });

    let block: WikiPageBlock = serde_json::from_value(backend_block).expect("should parse");
    assert_eq!(block.index, 0);
    assert_eq!(block.text, "验收期限为 30 日");
    assert_eq!(block.owner, "user");

    let serialised = serde_json::to_value(&block).unwrap();
    assert_eq!(serialised.get("index"), Some(&json!(0)));
    assert_eq!(serialised.get("text"), Some(&json!("验收期限为 30 日")));
    assert_eq!(serialised.get("owner"), Some(&json!("user")));
}

#[test]
fn a_page_block_without_the_extra_fields_still_parses() {
    /// The bridge must not depend on the backend sending every field -
    /// a block with only a text still belongs on the page.
    let block: WikiPageBlock = serde_json::from_value(json!({
        "text": "验收期限为 30 日",
    }))
    .expect("should parse");

    assert_eq!(block.index, 0);
    assert_eq!(block.text, "验收期限为 30 日");
    assert_eq!(block.owner, "");
}

#[test]
fn the_page_block_keeps_the_owner_it_was_given() {
    /// "model" and "user" mean different things to the reader: one
    /// paragraph is replaced by the next recompile, the other is not.
    let model: WikiPageBlock = serde_json::from_value(json!({
        "index": 1, "text": "模型写的", "owner": "model",
    }))
    .expect("should parse");
    let user: WikiPageBlock = serde_json::from_value(json!({
        "index": 2, "text": "用户写的", "owner": "user",
    }))
    .expect("should parse");

    assert_eq!(model.owner, "model");
    assert_eq!(user.owner, "user");
}

// ===== the commands TASK-050 added =====

#[test]
fn the_new_commands_read_the_backend_over_http() {
    let commands = include_str!("../src/commands.rs");
    // every one of them asks the backend for the page or the term
    assert!(commands.contains("/api/v1/wiki/pages/{}/blocks"));
    assert!(commands.contains("/api/v1/kb/terms/{}/context"));
    // the edit is a PUT with the body the backend expects
    assert!(commands.contains(r#".put(&url)"#));
    assert!(commands.contains(r#"json!({ "markdown": markdown })"#));
}

#[test]
fn the_new_commands_read_the_answer_the_backend_gives() {
    /// The key the answer is read from is the whole command: read the
    /// wrong one and the desktop app shows an empty page while the
    /// backend is answering perfectly well.
    let commands = include_str!("../src/commands.rs");
    assert!(
        command_body(commands, "list_page_blocks").contains(r#"body["blocks"]"#),
        "the paragraphs are not read from `blocks`"
    );
    assert!(
        command_body(commands, "get_term_context").contains("term"),
        "the term context is not read from the answer"
    );
}

#[test]
fn the_desktop_path_sends_the_terms_to_the_webview() {
    /// The terms travel with the answer: the bridge parses the
    /// backend's response and hands it to the webview as JSON. A field
    /// that only went in would lose every term on the way out.
    let response = ChatResponse {
        answer: "履约保证金按合同金额的百分之五缴纳。".to_string(),
        terms: vec!["履约保证金".to_string()],
        ..Default::default()
    };

    let serialised = serde_json::to_value(&response).unwrap();
    assert_eq!(serialised.get("terms"), Some(&json!(["履约保证金"])));
}

// ===== the commands TASK-051 added =====

/// The workspace is what the build runs over: the user adds the folders
/// and the files they want covered, and the build indexes what is in
/// there (AC-F-001).
const WORKSPACE_COMMANDS: [&str; 5] = [
    "list_workspace_entries",
    "add_workspace_folder",
    "add_workspace_file",
    "remove_workspace_entry",
    "rebuild_workspace",
];

#[test]
fn the_workspace_commands_are_all_registered() {
    let lib = include_str!("../src/lib.rs");
    for name in WORKSPACE_COMMANDS {
        assert!(
            COMMAND_NAMES.contains(&name),
            "command `{}` is not in the command list",
            name
        );
        assert!(
            lib.contains(name),
            "command `{}` is not registered in lib.rs",
            name
        );
    }
}

#[test]
fn the_workspace_commands_read_the_workspace_endpoints() {
    /// The paths are the whole contract with the backend: ask for the
    /// wrong one and the user sees an empty list instead of what they
    /// added.
    let commands = include_str!("../src/commands.rs");
    assert!(commands.contains("/api/v1/workspace/entries"));
    assert!(commands.contains("/api/v1/workspace/folders"));
    assert!(commands.contains("/api/v1/workspace/files"));
    assert!(commands.contains("/api/v1/ingest/rebuild"));
}

#[test]
fn the_workspace_commands_use_the_right_verbs() {
    /// A folder and a file are added with a POST, the list is read with
    /// a GET, and an entry is taken back out with a DELETE.
    let commands = include_str!("../src/commands.rs");
    assert!(command_body(commands, "list_workspace_entries").contains(".get(&url)"));
    assert!(command_body(commands, "add_workspace_folder").contains(".post(&url)"));
    assert!(command_body(commands, "add_workspace_file").contains(".post(&url)"));
    assert!(command_body(commands, "remove_workspace_entry").contains(".delete(&url)"));
    assert!(command_body(commands, "rebuild_workspace").contains(".post(&url)"));
}

#[test]
fn the_workspace_commands_send_the_path_the_user_picked() {
    /// The path is the whole request - a body without it adds nothing
    /// to the workspace.
    let commands = include_str!("../src/commands.rs");
    assert!(
        command_body(commands, "add_workspace_folder").contains(r#"json!({ "path": path })"#),
        "the folder is not sent as the path"
    );
    assert!(
        command_body(commands, "add_workspace_file").contains(r#"json!({ "path": path })"#),
        "the file is not sent as the path"
    );
}

#[test]
fn the_workspace_commands_check_the_status() {
    /// A 404 on "there is no such entry" must not look like a success -
    /// the entry would stay in the list forever, and the user would
    /// take it out again and again.
    let commands = include_str!("../src/commands.rs");
    for name in WORKSPACE_COMMANDS {
        let body = command_body(commands, name);
        assert!(
            body.contains("is_success()"),
            "command `{}` does not check the status of the response",
            name
        );
        assert!(
            body.contains(r#"Err(format!("HTTP {status}:"#),
            "command `{}` does not report the status code in its error",
            name
        );
    }
}

#[test]
fn the_rebuild_is_watched_through_the_ingest_status() {
    /// The rebuild answers with a job, and the job is polled with the
    /// endpoint the folder build already uses - the frontend needs no
    /// second polling path.
    let commands = include_str!("../src/commands.rs");
    assert!(command_body(commands, "get_ingest_status").contains("/api/v1/ingest/status/"));
}

#[test]
fn the_removal_survives_a_backend_that_answers_with_nothing() {
    /// A DELETE that answers with no body is still a removal - reading
    /// JSON out of an empty answer would turn a removal that worked
    /// into an error.
    let commands = include_str!("../src/commands.rs");
    assert!(
        command_body(commands, "remove_workspace_entry").contains("text.trim().is_empty()"),
        "an empty answer is not handled"
    );
}
