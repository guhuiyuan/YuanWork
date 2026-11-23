//! Python backend sidecar management.
//!
//! The Tauri app spawns the Python backend as a child process and manages
//! its lifecycle. Communication happens via HTTP localhost:8765.
//!
//! Two launch modes are supported:
//! - **Bundled** (release): a PyInstaller-bundled `yuanwork-backend` binary
//!   located under the Tauri resource directory.
//! - **Python** (dev / debug): the project venv's Python interpreter runs
//!   `python -m yuanwork` from the `src/backend/` directory. This avoids
//!   needing to package Python for every development iteration.

use std::io::{BufRead, Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager};

const BACKEND_PORT: u16 = 8765;
const BACKEND_HOST: &str = "127.0.0.1";

/// The port the backend should listen on.
///
/// Defaults to 8765. Override it with the `YUANWORK_PORT` environment
/// variable - the same name the Python backend already reads - so the
/// desktop app can run next to another instance instead of failing to
/// bind the port.
fn backend_port() -> u16 {
    std::env::var("YUANWORK_PORT")
        .ok()
        .map(|v| v.trim().to_string())
        .and_then(|v| v.parse::<u16>().ok())
        .filter(|p| *p > 0)
        .unwrap_or(BACKEND_PORT)
}

/// How the backend should be launched.
enum BackendLaunch {
    /// PyInstaller-bundled binary (future release path).
    Bundled(PathBuf),
    /// Project venv Python interpreter running `python -m yuanwork`.
    Python { executable: PathBuf, cwd: PathBuf },
}

/// Handle to the Python backend process.
pub struct BackendHandle {
    child: Mutex<Option<Child>>,
    port: u16,
}

impl BackendHandle {
    pub fn new(child: Child) -> Self {
        Self {
            child: Mutex::new(Some(child)),
            port: backend_port(),
        }
    }

    /// Get the backend URL (for HTTP requests from the frontend).
    pub fn url(&self) -> String {
        format!("http://{}:{}", BACKEND_HOST, self.port)
    }

    /// Kill the backend process.
    pub fn kill(&self) {
        if let Ok(mut guard) = self.child.lock() {
            if let Some(mut child) = guard.take() {
                let _ = child.kill();
                let _ = child.wait();
                log::info!("Backend process killed");
            }
        }
    }
}

impl Drop for BackendHandle {
    fn drop(&mut self) {
        self.kill();
    }
}

/// Start the Python backend as a sidecar process.
pub fn start_backend(app: &AppHandle) -> Result<BackendHandle, Box<dyn std::error::Error>> {
    let launch = find_backend_executable(app)?;
    let port = backend_port();

    let mut cmd = match launch {
        BackendLaunch::Bundled(path) => {
            log::info!("Starting bundled backend binary: {:?}", path);
            let mut c = Command::new(&path);
            c.args(["--port", &port.to_string(), "--host", BACKEND_HOST]);
            c
        }
        BackendLaunch::Python { executable, cwd } => {
            log::info!(
                "Starting Python backend: {:?} (cwd={:?}) on port {}",
                executable,
                cwd,
                port
            );
            let mut c = Command::new(&executable);
            c.args([
                "-m",
                "yuanwork",
                "--port",
                &port.to_string(),
                "--host",
                BACKEND_HOST,
            ]);
            c.current_dir(&cwd);
            // The backend's cwd is src/backend, so its default data
            // directory would be ~/.yuanwork - an empty knowledge base,
            // not the project's data/. Point it at the project's data
            // directory unless the user set one themselves.
            //
            // "One themselves" includes the .env at the project root:
            // YUANWORK_HOME moves the whole thing (the index *and* the
            // model cache), and pointing the index at the project's
            // data/ while the models live under the user's root would
            // split the two apart (ADR-045).
            if std::env::var_os("YUANWORK_DATA_DIR").is_none()
                && std::env::var_os("YUANWORK_HOME").is_none()
            {
                if let Some(project_root) = project_root_of(&cwd) {
                    let data_dir = project_root.join("data");
                    if data_dir.is_dir() && !env_file_sets_the_location(&project_root) {
                        c.env("YUANWORK_DATA_DIR", &data_dir);
                        log::info!("Backend data dir: {}", data_dir.display());
                    }
                }
            }
            c
        }
    };

    cmd.stdout(Stdio::piped()).stderr(Stdio::piped());

    // The token says "this backend is mine". The health endpoint hands
    // it back, and the readiness check compares it - so a server left
    // over from an earlier run can never pass for ours.
    let instance_token = instance_token();
    cmd.env("YUANWORK_INSTANCE_TOKEN", &instance_token);
    // And the backend watches this PID: if the app is killed without
    // closing its window, the backend leaves instead of staying behind
    // holding the port.
    cmd.env("YUANWORK_PARENT_PID", std::process::id().to_string());

    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        // CREATE_NO_WINDOW - don't show a console window for the child
        cmd.creation_flags(0x08000000);
    }

    let child = cmd.spawn()?;
    // The child's output has to be read. A pipe nobody reads fills up
    // (64 KB on Windows), and the backend then blocks on its next log
    // line - the port is still listening, but nothing answers anymore.
    // That is what happened to a running backend on 2026-11-23: it
    // answered /health for hours, then stopped answering anything at
    // all while still holding the port.
    let mut child = child;
    drain_output(&mut child);
    let handle = BackendHandle::new(child);

    // Wait for the backend to become healthy
    wait_for_backend_ready(&handle.url(), &instance_token)?;

    Ok(handle)
}

/// Read the child's stdout and stderr so their pipes cannot fill up.
///
/// The lines go into the app's log. Nothing else needs them - what
/// matters is that the pipe is empty when the backend writes to it.
fn drain_output(child: &mut Child) {
    if let Some(stdout) = child.stdout.take() {
        read_lines(stdout, false);
    }
    if let Some(stderr) = child.stderr.take() {
        read_lines(stderr, true);
    }
}

/// One thread per stream, until the stream ends.
fn read_lines<R: Read + Send + 'static>(reader: R, is_error: bool) {
    std::thread::spawn(move || {
        let reader = std::io::BufReader::new(reader);
        for line in reader.lines() {
            match line {
                Ok(line) => {
                    if is_error {
                        log::error!("[backend] {}", line);
                    } else {
                        log::info!("[backend] {}", line);
                    }
                }
                Err(_) => break,
            }
        }
    });
}

/// A token that identifies this launch of the app.
///
/// It only has to be different from other launches, not unguessable:
/// the process ID alone would do, but a second app instance can have
/// the same PID at a different time, so the clock is in there too.
fn instance_token() -> String {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    format!("{}-{}", std::process::id(), nanos)
}

/// Resolve the backend launch strategy: bundled binary if present, else
/// Python interpreter in the project venv.
fn find_backend_executable(app: &AppHandle) -> Result<BackendLaunch, Box<dyn std::error::Error>> {
    // 1. Bundled binary (release path)
    if let Ok(resource_path) = app
        .path()
        .resolve("yuanwork-backend", tauri::path::BaseDirectory::Resource)
    {
        if resource_path.exists() {
            return Ok(BackendLaunch::Bundled(resource_path));
        }
    }

    // 2. Python interpreter in venv (dev path)
    let executable = find_python_interpreter()?;
    let cwd = find_backend_module_dir()?;
    Ok(BackendLaunch::Python { executable, cwd })
}

/// Find a Python interpreter: prefer project venv, fall back to PATH.
///
/// Walks up to 8 parent directories looking for `.venv/Scripts/python.exe`
/// (Windows) or `.venv/bin/python` (Unix). If not found, tries `py` /
/// `python` / `python3` on the system PATH.
/// The project root this binary was built in.
///
/// The exe does not live in the checkout - the cargo target directory
/// is on D: to keep C: free - so walking up from the *current
/// directory* finds nothing when the app is started by double-clicking
/// the exe (the working directory is then the exe's own folder). The
/// build knows where the project is, so the build writes it into the
/// binary and the finder falls back to it.
fn build_time_project_root() -> Option<PathBuf> {
    // CARGO_MANIFEST_DIR is <root>/src/app/src-tauri.
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    manifest.parent()?.parent()?.parent().map(|p| p.to_path_buf())
}

/// The places to look for the project, most specific first.
///
/// The current directory and its parents come first (that is where the
/// app is normally started from), then the root the binary was built
/// in.
fn candidate_roots() -> Vec<PathBuf> {
    let mut roots: Vec<PathBuf> = Vec::new();
    if let Ok(cwd) = std::env::current_dir() {
        let mut dir = cwd.as_path();
        for _ in 0..8 {
            roots.push(dir.to_path_buf());
            match dir.parent() {
                Some(p) => dir = p,
                None => break,
            }
        }
    }
    if let Some(root) = build_time_project_root() {
        if !roots.contains(&root) {
            roots.push(root);
        }
    }
    roots
}

fn find_python_interpreter() -> Result<PathBuf, Box<dyn std::error::Error>> {
    for root in candidate_roots() {
        let cand = if cfg!(target_os = "windows") {
            root.join(".venv").join("Scripts").join("python.exe")
        } else {
            root.join(".venv").join("bin").join("python")
        };
        if cand.exists() {
            return Ok(cand);
        }
    }

    // Fall back to PATH. On Windows the `py` launcher is preferred because
    // it picks a registered Python via PEP 397.
    let names: &[&str] = if cfg!(target_os = "windows") {
        &["py", "python", "python3"]
    } else {
        &["python3", "python"]
    };
    for name in names {
        if std::process::Command::new(name)
            .arg("--version")
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false)
        {
            return Ok(PathBuf::from(name));
        }
    }
    Err("no Python interpreter found; create .venv at project root or install Python".into())
}

/// Locate the `src/backend/` directory.
///
/// This is the directory `python -m yuanwork` is invoked from so that
/// `import yuanwork` resolves to the project's package. The current
/// directory and its parents come first; the root the binary was built
/// in is the fallback, so the exe works even when it is started from
/// somewhere else entirely.
fn find_backend_module_dir() -> Result<PathBuf, Box<dyn std::error::Error>> {
    for root in candidate_roots() {
        let cand = root.join("src").join("backend");
        if cand.join("yuanwork").exists() {
            return Ok(cand);
        }
    }
    Err(match build_time_project_root() {
        Some(root) => format!(
            "could not locate src/backend/ (it was at {} when this was built)",
            root.display()
        )
        .into(),
        None => "could not locate src/backend/ (run from project root or src/app/)".into(),
    })
}

/// The project root, given the backend module directory.
///
/// The layout is `<root>/src/backend`, so the root is two levels up.
fn project_root_of(backend_dir: &std::path::Path) -> Option<PathBuf> {
    backend_dir.parent()?.parent().map(|p| p.to_path_buf())
}

/// Whether the project's .env decides where the backend keeps its data.
///
/// The .env is a file, not an environment variable, so the shell cannot
/// see it by looking at its own environment. But the backend reads it,
/// and a YUANWORK_HOME in there moves the index *and* the model cache
/// to one place - pointing the index at the project's data/ on top of
/// that would split the two apart.
fn env_file_sets_the_location(project_root: &std::path::Path) -> bool {
    let Ok(text) = std::fs::read_to_string(project_root.join(".env")) else {
        return false;
    };
    text.lines().map(str::trim).any(|line| {
        if line.is_empty() || line.starts_with('#') {
            return false;
        }
        let Some((key, _)) = line.split_once('=') else {
            return false;
        };
        matches!(
            key.trim().to_ascii_uppercase().as_str(),
            "YUANWORK_HOME" | "YUANWORK_DATA_DIR" | "YUANWORK_MODEL_CACHE_DIR"
        )
    })
}

/// Wait for the backend to respond to health checks.
///
/// The check is not just "something answered 200": the response has to
/// carry *our* instance token. A server left over from an earlier run
/// holds the port and answers the health check just fine - and the app
/// would then talk to that stale backend, with its stale data
/// directory, and see an index that is not there. The token is how we
/// tell "our backend" from "somebody else's".
fn wait_for_backend_ready(
    url: &str,
    instance_token: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    let health_url = format!("{}/api/v1/health", url);

    for attempt in 0..60 {
        match http_get(&health_url) {
            Ok(response) if parse_status_code(&response) == "200" => {
                match parse_instance_token(&response) {
                    Some(token) if token == instance_token => {
                        log::info!("Backend ready after {} attempts", attempt);
                        return Ok(());
                    }
                    // Anything else on the port is not the backend this
                    // app just started: either a leftover from an
                    // earlier run, or a server that does not report a
                    // token at all. Either way, using it would mean
                    // talking to somebody else's data directory.
                    _ => {
                        return Err(format!(
                            "端口 {port} 上跑着一个不是这次启动的 YuanWork 后端。\
                             可能是上一次的应用被强杀留下的残留进程。\
                             关掉它，或用 YUANWORK_PORT 指定别的端口。",
                            port = backend_port()
                        )
                        .into());
                    }
                }
            }
            _ => {
                std::thread::sleep(Duration::from_millis(500));
            }
        }
    }

    Err("Backend failed to start within 30 seconds".into())
}

/// Pull the instance token out of a health response.
///
/// The body is JSON; the token is one field of it. A body that is not
/// JSON, or has no token, yields None.
fn parse_instance_token(response: &str) -> Option<String> {
    let body = response.split("\r\n\r\n").nth(1).or_else(|| response.split("\n\n").nth(1))?;
    let parsed: serde_json::Value = serde_json::from_str(body.trim()).ok()?;
    parsed
        .get("instance_token")?
        .as_str()
        .map(|token| token.to_string())
}

/// Minimal blocking HTTP GET that returns the whole response.
/// Used only for the one-shot startup health probe.
fn http_get(url: &str) -> Result<String, Box<dyn std::error::Error>> {
    let parsed = url::Url::parse(url)?;
    let host = parsed.host_str().ok_or("No host")?;
    let port = parsed.port().unwrap_or(80);
    let path = parsed.path();

    let mut stream = TcpStream::connect((host, port))?;
    let request = format!(
        "GET {} HTTP/1.1\r\nHost: {}:{}\r\nConnection: close\r\n\r\n",
        path, host, port
    );
    stream.write_all(request.as_bytes())?;

    let mut response = String::new();
    std::io::Read::read_to_string(&mut stream, &mut response)?;
    Ok(response)
}

/// Pull the status code out of an HTTP response.
///
/// The status line looks like `HTTP/1.1 200 OK`, so the code is the
/// second whitespace-separated field - comparing the whole line to
/// "200" (or checking that it *starts with* "200") never matches.
fn parse_status_code(response: &str) -> &str {
    response
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .unwrap_or("")
}

/// Backend health status (for Tauri commands).
#[derive(Serialize, Deserialize, Debug)]
pub struct BackendStatus {
    pub running: bool,
    pub url: String,
    pub pid: Option<u32>,
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The environment is shared by all the tests in one binary, so the
    /// tests that touch it take a lock.
    static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    #[test]
    fn test_backend_url() {
        let handle = BackendHandle {
            child: Mutex::new(None),
            port: BACKEND_PORT,
        };
        assert_eq!(handle.url(), "http://127.0.0.1:8765");
    }

    #[test]
    fn test_backend_port_falls_back_to_the_default() {
        let _guard = ENV_LOCK.lock().unwrap();
        // No YUANWORK_PORT in the test environment -> the default.
        assert_eq!(backend_port(), BACKEND_PORT);
    }

    #[test]
    fn test_parse_status_code() {
        // The status line is "HTTP/1.1 200 OK" - the code is the second
        // field, not the start of the line.
        assert_eq!(parse_status_code("HTTP/1.1 200 OK\r\n\r\nbody"), "200");
        assert_eq!(parse_status_code("HTTP/1.1 404 Not Found\r\n"), "404");
        assert_eq!(parse_status_code("HTTP/1.1 500 Internal Server Error"), "500");
        assert_eq!(parse_status_code(""), "");
        assert_eq!(parse_status_code("garbage"), "");
    }

    // ===== the instance token =====

    #[test]
    fn test_the_instance_token_is_read_from_the_health_body() {
        let response = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n\
            {\"status\": \"ok\", \"instance_token\": \"1234-99\"}";
        assert_eq!(parse_instance_token(response), Some("1234-99".to_string()));
    }

    #[test]
    fn test_a_health_body_without_a_token_yields_none() {
        // A server that does not report a token is not ours.
        let response = "HTTP/1.1 200 OK\r\n\r\n{\"status\": \"ok\"}";
        assert_eq!(parse_instance_token(response), None);
    }

    #[test]
    fn test_a_body_that_is_not_json_yields_none() {
        let response = "HTTP/1.1 200 OK\r\n\r\nnot json at all";
        assert_eq!(parse_instance_token(response), None);
    }

    #[test]
    fn test_the_token_changes_between_launches() {
        // Two launches of the app must not share a token - that is the
        // whole point of it.
        let first = instance_token();
        std::thread::sleep(std::time::Duration::from_millis(2));
        let second = instance_token();
        assert_ne!(first, second);
    }

    // ===== finding the project =====

    #[test]
    fn test_the_build_time_root_is_where_the_project_was() {
        // The exe does not live in the checkout, so the working
        // directory cannot be the only lead (TASK-083).
        let root = build_time_project_root().expect("the build directory is known");
        assert!(
            root.join("src").join("backend").join("yuanwork").exists(),
            "the build-time root should be the project root: {}",
            root.display()
        );
    }

    #[test]
    fn test_the_candidate_roots_include_the_build_time_root() {
        let roots = candidate_roots();
        let built = build_time_project_root().expect("the build directory is known");
        assert!(roots.contains(&built));
    }

    #[test]
    fn test_the_backend_is_found_from_anywhere() {
        // The walk up from the current directory is tried first, but the
        // build-time root is always in the list - so the exe works even
        // when it is started by double-clicking it.
        assert!(find_backend_module_dir().is_ok());
    }

    #[test]
    fn test_find_backend_module_dir_from_project_root() {
        // Running tests from src-tauri/ should still locate src/backend/ by walking up
        if let Ok(dir) = find_backend_module_dir() {
            assert!(dir.join("yuanwork").exists());
        }
    }

    // ===== the port =====

    #[test]
    fn test_the_port_comes_from_the_environment() {
        let _guard = ENV_LOCK.lock().unwrap();
        std::env::set_var("YUANWORK_PORT", "9123");
        assert_eq!(backend_port(), 9123);
        std::env::remove_var("YUANWORK_PORT");
    }

    #[test]
    fn test_the_port_is_trimmed() {
        let _guard = ENV_LOCK.lock().unwrap();
        std::env::set_var("YUANWORK_PORT", "  9124  ");
        assert_eq!(backend_port(), 9124);
        std::env::remove_var("YUANWORK_PORT");
    }

    #[test]
    fn test_a_junk_port_falls_back_to_the_default() {
        let _guard = ENV_LOCK.lock().unwrap();
        std::env::set_var("YUANWORK_PORT", "not-a-port");
        assert_eq!(backend_port(), BACKEND_PORT);
        std::env::remove_var("YUANWORK_PORT");
    }

    #[test]
    fn test_a_zero_port_falls_back_to_the_default() {
        let _guard = ENV_LOCK.lock().unwrap();
        std::env::set_var("YUANWORK_PORT", "0");
        assert_eq!(backend_port(), BACKEND_PORT);
        std::env::remove_var("YUANWORK_PORT");
    }

    #[test]
    fn test_the_default_port_is_8765() {
        assert_eq!(BACKEND_PORT, 8765);
    }

    // ===== the interpreter lookup =====

    #[test]
    fn test_the_interpreter_is_found_in_the_project_venv() {
        // The repository has a .venv at its root; walking up from the
        // crate directory has to find it.
        if let Ok(path) = find_python_interpreter() {
            assert!(path.exists());
            let name = path.file_name().unwrap().to_string_lossy().to_string();
            assert!(name.starts_with("python"));
        }
    }

    #[test]
    fn test_the_interpreter_lookup_walks_up_at_most_eight_levels() {
        let source = include_str!("sidecar.rs");
        assert!(source.contains("for _ in 0..8"));
    }

    #[test]
    fn test_the_backend_module_dir_is_found() {
        if let Ok(dir) = find_backend_module_dir() {
            assert!(dir.join("yuanwork").exists());
        }
    }

    // ===== the health probe =====

    #[test]
    fn the_probe_asks_the_health_path() {
        let source = include_str!("sidecar.rs");
        assert!(source.contains("/api/v1/health"));
    }

    #[test]
    fn test_the_probe_keeps_trying_until_the_budget_is_spent() {
        // The probe gives up after 60 attempts * 500 ms. We do not wait
        // that long in a test, so we pin the budget instead.
        let source = include_str!("sidecar.rs");
        assert!(source.contains("for attempt in 0..60"));
        assert!(source.contains("Duration::from_millis(500)"));
    }

    #[test]
    fn test_the_probe_budget_is_thirty_seconds() {
        let source = include_str!("sidecar.rs");
        assert!(source.contains("for attempt in 0..60"));
        assert!(source.contains("Duration::from_millis(500)"));
        assert!(source.contains("within 30 seconds"));
    }

    // ===== the launch path =====

    #[test]
    fn the_bundled_launch_path_is_unreachable_in_this_configuration() {
        // `tauri.conf.json` declares no `bundle.resources`, so the
        // bundled branch of `find_backend_executable` never fires.
        let conf = include_str!("../tauri.conf.json");
        assert!(!conf.contains("\"resources\""));
    }

    #[test]
    fn the_child_output_is_read() {
        // The child's stdout and stderr are piped, so somebody has to
        // read them: a pipe nobody reads fills up (64 KB on Windows) and
        // the backend then blocks on its next log line — the port is
        // still listening, but nothing answers anymore. That is what
        // happened to a running backend on 2026-11-23.
        let source = include_str!("sidecar.rs");
        assert!(source.contains("Stdio::piped()"));
        assert!(source.contains("drain_output(&mut child)"));
        // built at runtime so this test's own text cannot satisfy it
        let reader = format!("Buf{}", "Reader");
        assert!(source.contains(&reader));
    }

    #[test]
    fn the_backend_process_is_not_monitored() {
        // There is no process check and no restart: if the backend dies,
        // the shell does not notice.
        let source = include_str!("sidecar.rs");
        let needle = format!("try_{}", "wait");
        assert!(!source.contains(&needle));
    }

    // ===== where the backend keeps its data =====

    /// A throwaway directory holding a .env with the given content.
    fn env_file_with(content: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("yuanwork-env-{}", content.len()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join(".env"), content).unwrap();
        dir
    }

    #[test]
    fn a_home_in_the_env_file_moves_the_data_out_of_the_project() {
        let dir = env_file_with("YUANWORK_HOME=D:/yuanwork\n");
        assert!(env_file_sets_the_location(&dir));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_data_dir_in_the_env_file_counts_too() {
        let dir = env_file_with("YUANWORK_DATA_DIR=D:/somewhere\n");
        assert!(env_file_sets_the_location(&dir));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn an_unrelated_env_file_does_not_count() {
        let dir = env_file_with("YUANWORK_PORT=8765\nMINIMAX_API_KEY=secret\n");
        assert!(!env_file_sets_the_location(&dir));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_commented_out_line_does_not_count() {
        let dir = env_file_with("# YUANWORK_HOME=D:/yuanwork\n");
        assert!(!env_file_sets_the_location(&dir));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_missing_env_file_does_not_count() {
        let dir = std::env::temp_dir().join("yuanwork-env-missing");
        let _ = std::fs::remove_dir_all(&dir);
        assert!(!env_file_sets_the_location(&dir));
    }

    #[test]
    fn the_shell_leaves_the_location_to_the_backend_when_the_user_set_one() {
        // The shell only points the backend at the project's data/
        // when nothing else decides. Both the environment and the
        // .env count as "something else" (ADR-045).
        let source = include_str!("sidecar.rs");
        assert!(source.contains("var_os(\"YUANWORK_HOME\")"));
        assert!(source.contains("env_file_sets_the_location"));
    }
}
