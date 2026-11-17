"""YuanWork backend entry point.

Run with: python -m yuanwork [--port 8765] [--host 127.0.0.1]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import uvicorn


def _configure_stdio_for_unicode() -> None:
    """Reconfigure stdout/stderr to UTF-8 so emojis/logs render on Windows.

    On Windows the default console code page is often GBK, which can't encode
    the 🚀 emoji or many log characters. Python 3.7+ supports reconfigure().
    We ignore errors so the program still starts if reconfigure isn't
    available (e.g. when stdout is not a real TTY).
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # Stream may already be closed or non-configurable
            pass


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        prog="yuanwork",
        description="YuanWork backend server",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Bind host (default: from env YUANWORK_HOST or 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind port (default: from env YUANWORK_PORT or 8765)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload (development only)",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="Log level (default: from env YUANWORK_LOG_LEVEL or INFO)",
    )
    return parser.parse_args()


def main() -> None:
    """Main entry point."""
    # Must run before any print() call
    _configure_stdio_for_unicode()

    args = parse_args()

    # Ensure the backend root is in sys.path for relative imports
    backend_root = Path(__file__).parent.parent
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))

    # Lazy import to avoid circular deps and speed up --help
    from yuanwork.api.app import create_app
    from yuanwork.config import get_settings
    from yuanwork.privacy.network_guard import NetworkGuard

    settings = get_settings()
    app = create_app(settings=settings)

    # Install network isolation guard at the socket layer (Stage 3).
    # In ALLOW_ALL mode this is a no-op. In LLM_ONLY / OFFLINE, only
    # whitelisted domains (and localhost for LLM_ONLY) can be reached.
    guard = NetworkGuard.from_settings()
    if guard.install():
        print(f"   Network guard: ACTIVE ({guard.mode})")
    else:
        print(f"   Network guard: disabled (mode={guard.mode})")

    host = args.host or settings.host
    port = args.port or settings.port
    log_level = (args.log_level or settings.log_level).lower()

    print(f"Starting YuanWork backend at http://{host}:{port}")
    print(f"   Data dir: {settings.data_dir}")
    print(f"   Model cache: {settings.model_cache_dir}")
    print(f"   Network mode: {settings.network_mode.name}")
    print(f"   Log level: {log_level}")

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
