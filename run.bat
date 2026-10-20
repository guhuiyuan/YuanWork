@echo off
REM Launch the RELEASE Tauri build with the correct working directory.
REM The Tauri sidecar walks up from cwd to find src/backend/ + .venv/,
REM so we must launch from the project root, not from the .exe's directory.
REM
REM Default: release build. Override by passing the path:
REM   run.bat  -> release
REM   run.bat debug   -> debug
REM   run.bat "D:\path\to\yuanwork.exe"

setlocal
set "EXE=D:\cargo-target\release\yuanwork.exe"
if /I "%1"=="debug" set "EXE=D:\cargo-target\debug\yuanwork.exe"
if /I "%1"=="release" shift
if not "%1"=="" if exist "%~1" set "EXE=%~1"

if not exist "%EXE%" (
    echo yuanwork.exe not found: %EXE%
    echo Build first: cargo build --release ^(in src/app/src-tauri/^)
    exit /b 1
)

cd /d "%~dp0"
"%EXE%" %*
