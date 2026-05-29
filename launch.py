#!/usr/bin/env python3
"""
launch.py  —  Cryptogam Digitizationizer 2000  ·  local web-server launcher
----------------------------------------------------------------------------
Starts the FastAPI server on localhost:8000 and immediately opens the default
browser to that address.

Usage
-----
    python launch.py                  # production (no auto-reload)
    python launch.py --reload         # development (auto-reload on file save)
    python launch.py --port 9000      # custom port

The desktop Tkinter app is still available by running segmenter_gui.py.
Both can run simultaneously — they don't share any mutable state.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Launch the Digitizationizer web server")
    p.add_argument("--host",   default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    p.add_argument("--port",   default=8000, type=int, help="TCP port (default: 8000)")
    p.add_argument("--reload", action="store_true",   help="Enable uvicorn auto-reload")
    p.add_argument("--no-browser", action="store_true",
                   help="Start server without opening the browser")
    return p.parse_args()


def _open_browser(url: str, delay: float = 1.5) -> None:
    """Wait briefly for the server to start, then open *url* in the browser."""
    time.sleep(delay)
    webbrowser.open(url)


def main() -> None:
    args   = _parse_args()
    url    = f"http://{args.host}:{args.port}"

    print("=" * 60)
    print("  Cryptogam Digitizationizer 2000  —  Web Server")
    print("=" * 60)
    print(f"  Listening on  {url}")
    print(f"  Auto-reload   {'ON' if args.reload else 'OFF'}")
    print()
    print("  Endpoints:")
    print(f"    POST  {url}/api/preview   (detect, no files written)")
    print(f"    POST  {url}/api/batch     (full batch, SSE stream)")
    print(f"    POST  {url}/api/fix       (re-segment / bisect crop)")
    print(f"    GET   {url}/api/crops     (list output folder)")
    print(f"    GET   {url}/docs          (interactive API docs)")
    print()
    print("  Press Ctrl+C to stop.")
    print("=" * 60)

    if not args.no_browser:
        t = threading.Thread(target=_open_browser, args=(url,), daemon=True)
        t.start()

    try:
        import uvicorn
    except ImportError:
        print(
            "\nERROR: uvicorn is not installed.\n"
            "  pip install uvicorn[standard]\n",
            file=sys.stderr,
        )
        sys.exit(1)

    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
