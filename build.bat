@echo off
REM Build the YuanWork desktop release binary.
REM
REM IMPORTANT: the `custom-protocol` feature is what makes Tauri embed the
REM built frontend (dist/) into the binary. Without it the app runs in
REM "dev mode" and tries to load http://localhost:5173 instead - you get a
REM blank window unless a Vite dev server happens to be running.
REM `cargo build --release` alone does NOT enable it; `npx tauri build` does.
REM
REM Usage:
REM   build.bat            release build (default)
REM   build.bat debug      debug build
REM
REM Env overrides:
REM   CARGO_TARGET_DIR     where cargo puts the artifacts (default D:\cargo-target)

setlocal
set "MODE=%1"
if /I "%MODE%"=="" set "MODE=release"
if /I "%MODE%"=="debug" goto :debug
if /I "%MODE%"=="release" goto :release

echo Unknown mode: %MODE%  (expected "release" or "debug")
exit /b 1

:release
set "PROFILE_ARGS=--release"
set "PROFILE_DIR=release"
goto :build

:debug
set "PROFILE_ARGS="
set "PROFILE_DIR=debug"
goto :build

:build
set "ROOT=%~dp0"
set "CARGO_TARGET_DIR=%CARGO_TARGET_DIR%"
if "%CARGO_TARGET_DIR%"=="" set "CARGO_TARGET_DIR=D:\cargo-target"

REM 1. Build the frontend. Tauri embeds whatever is in src/app/dist.
echo === [1/3] building frontend ===
pushd "%ROOT%src\app"
call npm run build
if errorlevel 1 (
    echo Frontend build failed.
    popd
    exit /b 1
)
popd

REM 2. Force the Rust crate to recompile. Cargo does not notice that
REM    src/app/dist changed: tauri-build never emits a rerun-if-changed
REM    for the frontend directory, and the hashed asset names change
REM    without changing dist/\'s own mtime. Without this the binary keeps
REM    the previous frontend baked in.
echo === [2/3] forcing rebuild of the Tauri crate ===
pushd "%ROOT%src\app\src-tauri"
powershell -NoProfile -Command "(Get-Item 'src\lib.rs').LastWriteTime = Get-Date"
if errorlevel 1 (
    echo Could not touch src\lib.rs - the binary may embed a stale frontend.
    popd
    exit /b 1
)
popd

REM 3. Build the binary with the frontend embedded.
echo === [3/3] cargo build %PROFILE_ARGS% --features custom-protocol ===
pushd "%ROOT%src\app\src-tauri"
cargo build %PROFILE_ARGS% --features custom-protocol
if errorlevel 1 (
    echo Cargo build failed.
    popd
    exit /b 1
)
popd

echo.
echo Built: %CARGO_TARGET_DIR%\%PROFILE_DIR%\yuanwork.exe
echo Run it with: run.bat
exit /b 0
