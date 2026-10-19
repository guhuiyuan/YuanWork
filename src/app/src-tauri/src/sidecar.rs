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
            port: BACKEND_PORT,
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

    let mut cmd = match launch {
        BackendLaunch::Bundled(path) => {
            log::info!("Starting bundled backend binary: {:?}", path);
            let mut c = Command::new(&path);
            c.args(["--port", &BACKEND_PORT.to_string(), "--host", BACKEND_HOST]);
            c
        }
        BackendLaunch::Python { executable, cwd } => {
            log::info!(
                "Starting Python backend: {:?} (cwd={:?})",
                executable,
                cwd
            );
            let mut c = Command::new(&executable);
            c.args([
                "-m",
                "yuanwork",
                "--port",
                &BACKEND_PORT.to_string(),
                "--host",
                BACKEND_HOST,
            ]);
            c.current_dir(&cwd);
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

/// Wait for the backend to respond to health checks.
fn wait_for_backend_ready(url: &str) -> Result<(), Box<dyn std::error::Error>> {
    let health_url = format!("{}/api/v1/health", url);

    for attempt in 0..60 {
        match reqwest_get(&health_url) {
            Ok(response) if response.starts_with("200") => {
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

/// Minimal blocking HTTP GET that returns the status line as text.
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
    Ok(response.lines().next().unwrap_or("").to_string())
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

    #[test]
    fn test_backend_url() {
        let handle = BackendHandle {
            child: Mutex::new(None),
            port: BACKEND_PORT,
        };
        assert_eq!(handle.url(), "http://127.0.0.1:8765");
    }

    #[test]
    fn test_find_backend_module_dir_from_project_root() {
        // Running tests from src-tauri/ should still locate src/backend/ by walking up
        if let Ok(dir) = find_backend_module_dir() {
            assert!(dir.join("yuanwork").exists());
        }
    }
}
