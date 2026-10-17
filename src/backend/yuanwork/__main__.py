"""YuanWork backend entry point.

Run with: python -m yuanwork [--port 8765] [--host 127.0.0.1]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import uvicorn


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
    args = parse_args()

    # Ensure the backend root is in sys.path for relative imports
    backend_root = Path(__file__).parent.parent
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))

    # Lazy import to avoid circular deps and speed up --help
    from yuanwork.api.app import create_app
    from yuanwork.config import get_settings

    settings = get_settings()
    app = create_app(settings=settings)

    host = args.host or settings.host
    port = args.port or settings.port
    log_level = (args.log_level or settings.log_level).lower()

    print(f"🚀 Starting YuanWork backend at http://{host}:{port}")
    print(f"   Data dir: {settings.data_dir}")
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
