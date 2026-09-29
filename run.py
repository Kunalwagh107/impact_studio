"""Impact Studio launcher.

    python run.py                 # start the app on http://127.0.0.1:8777
    python run.py --port 9000
    python run.py --no-browser
    python run.py --port 0        # let the OS pick a free port
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _port_owner(port: int) -> str | None:
    """Describe what is already listening on ``port``, if anything.

    Returns a short human string, or None when the port is free. Best-effort:
    this is a diagnostic message, so failure to identify the owner is fine.
    """
    for conn in _netstat():
        if f":{port} " in conn and "LISTENING" in conn:
            pid = conn.split()[-1]
            who = _pid_name(pid)
            return f"PID {pid}{f' ({who})' if who else ''}"
    return None


def _netstat() -> list[str]:
    import subprocess
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True, timeout=10).stdout
        return out.splitlines()
    except Exception:
        return []


def _pid_name(pid: str) -> str | None:
    import subprocess
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV"],
                             capture_output=True, text=True, timeout=10).stdout
        rows = [l for l in out.splitlines() if l.startswith('"')]
        return rows[-1].split('","')[0].strip('"') if rows else None
    except Exception:
        return None


def _say(msg: str = "") -> None:
    """Print immediately.

    Stdout is block-buffered when it is redirected to a file or a pipe, so
    without an explicit flush these warnings can be lost entirely - and a
    silent port change is worse than a crash, because the user is left
    wondering why their browser opened the wrong URL.
    """
    print(msg, flush=True)


def _pick_port(host: str, wanted: int) -> int:
    """Return ``wanted`` if free, otherwise the next free port above it.

    A port left bound by an earlier run is the common case here, and the raw
    WinError 10048 that uvicorn raises for it tells the user nothing useful.
    """
    owner = _port_owner(wanted)
    if owner is None:
        return wanted
    _say(f"  ! port {wanted} is already in use by {owner}.")
    _say("    This is usually an Impact Studio server you left running.")
    _say(f"    Stop it with:  taskkill /F /PID {owner.split()[1]}")
    for candidate in range(wanted + 1, wanted + 20):
        if _port_owner(candidate) is None:
            _say(f"    Starting on port {candidate} instead "
                 "(override with --port).")
            return candidate
    _say("    No free port found in the next 20. Pass --port to choose one.")
    raise SystemExit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description="Impact Studio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()

    # port 0 means "let the OS choose"; skip the occupancy dance entirely
    if args.port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((args.host, 0))
            args.port = s.getsockname()[1]
    else:
        args.port = _pick_port(args.host, args.port)

    url = f"http://{args.host}:{args.port}/"

    if not args.no_browser:
        def _open():
            time.sleep(1.4)
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()

    print("=" * 68, flush=True)
    print("  Impact Studio - interactive data-update impact studies", flush=True)
    print(f"  {url}", flush=True)
    print("  Outputs are written to ./outputs   |   Ctrl+C to stop", flush=True)
    print("=" * 68, flush=True)

    import uvicorn

    try:
        uvicorn.run(
            "backend.main:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
            log_level="info",
        )
    except OSError as exc:
        # last-resort guard: the port was taken between the check and the bind
        if getattr(exc, "errno", None) in (48, 98, 10048):
            print(f"\n  Port {args.port} was taken while starting. "
                  "Pick another with --port, or stop the process holding it.")
            raise SystemExit(1)
        raise


if __name__ == "__main__":
    main()
