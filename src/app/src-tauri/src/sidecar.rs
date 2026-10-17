//! Python backend sidecar management.
//!
//! The Tauri app spawns the Python backend as a child process and manages
//! its lifecycle. Communication happens via HTTP localhost:8765.

use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager};

const BACKEND_PORT: u16 = 8765;
const BACKEND_HOST: &str = "127.0.0.1";

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
    // Determine the path to the Python backend executable
    let backend_path = find_backend_executable(app)?;

    log::info!("Starting backend: {:?}", backend_path);

    let mut cmd = Command::new(&backend_path);
    cmd.args([
        "--port", &BACKEND_PORT.to_string(),
        "--host", BACKEND_HOST,
    ]);

    cmd.stdout(Stdio::piped());
    cmd.stderr(Stdio::piped());

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

/// Find the Python backend executable.
fn find_backend_executable(app: &AppHandle) -> Result<std::path::PathBuf, Box<dyn std::error::Error>> {
    // In production: use the bundled externalBin
    // In development: use python -m yuanwork

    // First, check if there's a bundled binary (production)
    if let Ok(resource_path) = app
        .path()
        .resolve("yuanwork-backend", tauri::path::BaseDirectory::Resource)
    {
        if resource_path.exists() {
            return Ok(resource_path);
        }
    }

    // Development fallback: use python -m yuanwork
    // We need to find the python executable
    let python = find_python()?;

    // Use the project root as cwd so the backend can find its modules
    let cwd = std::env::current_dir()?;
    let module_path = cwd.join("src").join("backend");

    Ok(python.with_arg("-m").with_arg("yuanwork").with_cwd(module_path))
}

/// Find a Python executable.
fn find_python() -> Result<CommandWrapper, Box<dyn std::error::Error>> {
    // Try python3, python, then python3.11/3.12
    for cmd in &["python3", "python", "python3.11", "python3.12"] {
        if Command::new(cmd).arg("--version").output().is_ok() {
            return Ok(CommandWrapper::new(cmd));
        }
    }
    Err("Python not found in PATH".into())
}

/// Lightweight wrapper around Command for "python -m yuanwork".
struct CommandWrapper {
    cmd: String,
    args: Vec<String>,
}

impl CommandWrapper {
    fn new(cmd: &str) -> Self {
        Self { cmd: cmd.to_string(), args: vec![] }
    }

    fn with_arg(mut self, arg: &str) -> Self {
        self.args.push(arg.to_string());
        self
    }

    fn with_cwd(self, _cwd: std::path::PathBuf) -> Self {
        // TODO: set working directory
        self
    }

    fn as_command(&self) -> Command {
        let mut c = Command::new(&self.cmd);
        for arg in &self.args {
            c.arg(arg);
        }
        c
    }
}

impl std::fmt::Debug for CommandWrapper {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} {}", self.cmd, self.args.join(" "))
    }
}

/// Wait for the backend to respond to health checks.
fn wait_for_backend_ready(url: &str) -> Result<(), Box<dyn std::error::Error>> {
    use std::io::{Read, Write};
    use std::net::TcpStream;

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

/// Simple HTTP GET helper (avoid pulling in reqwest just for startup check).
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
}
