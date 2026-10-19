@echo off
REM Launch the debug Tauri build with the correct working directory.
REM The Tauri sidecar walks up from cwd to find src/backend/ + .venv/,
REM so we must launch from the project root, not from the .exe's directory.

cd /d "%~dp0"
"D:\cargo-target\debug\yuanwork.exe" %*
