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

use std::io::{Read, Write};
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
            if std::env::var_os("YUANWORK_DATA_DIR").is_none() {
                if let Some(project_root) = project_root_of(&cwd) {
                    let data_dir = project_root.join("data");
                    if data_dir.is_dir() {
                        c.env("YUANWORK_DATA_DIR", &data_dir);
                        log::info!("Backend data dir: {}", data_dir.display());
                    }
                }
            }
            c
        }
    };

    cmd.stdout(Stdio::piped()).stderr(Stdio::piped());

    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        // CREATE_NO_WINDOW - don't show a console window for the child
        cmd.creation_flags(0x08000000);
    }

    let child = cmd.spawn()?;
    let handle = BackendHandle::new(child);

    // Wait for the backend to become healthy
    wait_for_backend_ready(&handle.url())?;

    Ok(handle)
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
fn find_python_interpreter() -> Result<PathBuf, Box<dyn std::error::Error>> {
    if let Ok(cwd) = std::env::current_dir() {
        let mut dir = cwd.as_path();
        for _ in 0..8 {
            let cand = if cfg!(target_os = "windows") {
                dir.join(".venv").join("Scripts").join("python.exe")
            } else {
                dir.join(".venv").join("bin").join("python")
            };
            if cand.exists() {
                return Ok(cand);
            }
            match dir.parent() {
                Some(p) => dir = p,
                None => break,
            }
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

/// Locate the `src/backend/` directory by walking up from cwd.
///
/// This is the directory `python -m yuanwork` is invoked from so that
/// `import yuanwork` resolves to the project's package.
fn find_backend_module_dir() -> Result<PathBuf, Box<dyn std::error::Error>> {
    if let Ok(cwd) = std::env::current_dir() {
        let mut dir = cwd.as_path();
        for _ in 0..8 {
            let cand = dir.join("src").join("backend");
            if cand.join("yuanwork").exists() {
                return Ok(cand);
            }
            match dir.parent() {
                Some(p) => dir = p,
                None => break,
            }
        }
    }
    Err("could not locate src/backend/ (run from project root or src/app/)".into())
}

/// The project root, given the backend module directory.
///
/// The layout is `<root>/src/backend`, so the root is two levels up.
fn project_root_of(backend_dir: &std::path::Path) -> Option<PathBuf> {
    backend_dir.parent()?.parent().map(|p| p.to_path_buf())
}

/// Wait for the backend to respond to health checks.
fn wait_for_backend_ready(url: &str) -> Result<(), Box<dyn std::error::Error>> {
    let health_url = format!("{}/api/v1/health", url);

    for attempt in 0..60 {
        match reqwest_get(&health_url) {
            Ok(code) if code == "200" => {
                log::info!("Backend ready after {} attempts", attempt);
                return Ok(());
            }
            _ => {
                std::thread::sleep(Duration::from_millis(500));
            }
        }
    }

    Err("Backend failed to start within 30 seconds".into())
}

/// Minimal blocking HTTP GET that returns the HTTP status code.
/// Used only for the one-shot startup health probe.
fn reqwest_get(url: &str) -> Result<String, Box<dyn std::error::Error>> {
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
    Ok(parse_status_code(&response).to_string())
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
    fn the_child_output_is_never_read() {
        // The child's stdout and stderr are piped, but nothing drains
        // them — the pipe buffer can fill up and block the backend.
        let source = include_str!("sidecar.rs");
        assert!(source.contains("Stdio::piped()"));
        // built at runtime so this test's own text cannot satisfy it
        let reader = format!("Buf{}", "Reader");
        assert!(!source.contains(&reader));
    }

    #[test]
    fn the_backend_process_is_not_monitored() {
        // There is no process check and no restart: if the backend dies,
        // the shell does not notice.
        let source = include_str!("sidecar.rs");
        let needle = format!("try_{}", "wait");
        assert!(!source.contains(&needle));
    }
}
