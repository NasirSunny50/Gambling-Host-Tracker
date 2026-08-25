"""Start the read-only web portal.

A convenience entry point so the portal can be launched with a plain interpreter path,
without the `ght` console script being on PATH:

    .venv\\Scripts\\python.exe scripts/serve.py            # default http://127.0.0.1:8000
    .venv\\Scripts\\python.exe scripts/serve.py --port 9000

If it is started with the wrong Python (the system one instead of the project virtualenv),
it says so and prints the command to use, instead of a bare import traceback.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _port_is_free(host: str, port: int) -> bool:
    """Whether the portal can actually bind this port right now.

    A plain "is anything listening?" check is not enough on Windows: a port can be *bind*
    reserved by Hyper-V / WSL / Docker (an "excluded port range") with nothing listening on
    it, and binding still fails with WinError 10048. Trying the bind is the only answer that
    matches what uvicorn is about to do, so that is what we do.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
            return True
        except OSError:
            return False


def _resolve_port(host: str, port: int) -> int:
    """The wanted port if it is free, otherwise the next free one above it.

    So a stale portal that would not die, or a port the OS has reserved out from under us,
    can no longer stop the portal from starting - it just moves to the next open door and
    the browser is pointed there. Falls back to the wanted port if nothing nearby is free,
    letting uvicorn report the bind error itself.
    """
    if _port_is_free(host, port):
        return port
    for candidate in range(port + 1, port + 51):
        if _port_is_free(host, candidate):
            return candidate
    return port


def _bootstrap() -> None:
    """Put src/ on sys.path, and fail with a clear message on the wrong interpreter."""
    src = REPO_ROOT / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))
    try:
        # Import the route module, not just the package: it pulls in fastapi, the models
        # and pydantic-settings, so a missing dependency is caught here with a clear
        # message instead of crashing later inside uvicorn's startup.
        import uvicorn  # noqa: F401

        from ght.api import routes  # noqa: F401
    except ImportError:
        venv = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
        script = Path(__file__).relative_to(REPO_ROOT)
        hint = (
            f"  {venv.relative_to(REPO_ROOT)} {script}"
            if venv.exists()
            else '  pip install -e ".[api]"'
        )
        sys.exit(
            f"Cannot import the portal with {Path(sys.executable).name} ({sys.executable}).\n"
            "The portal and its dependencies live in this project's virtualenv. Run it with:\n"
            f"{hint}\n"
            'If the virtualenv does not exist yet:  python -m venv .venv  then  '
            'pip install -e ".[api]"'
        )


def main() -> int:
    _bootstrap()

    parser = argparse.ArgumentParser(description="Run the Host Tracker portal.")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default loopback)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="restart on code changes")
    parser.add_argument("--open", action="store_true",
                        help="open the portal in the default browser once it is up")
    args = parser.parse_args()

    import uvicorn

    # Reload runs this file twice (a supervisor plus the worker), so the port hunt and the
    # browser open would happen in the wrong process. Reload is a developer flag and never
    # used by Start.bat, so keep it simple: only hunt/open when not reloading.
    port = args.port if args.reload else _resolve_port(args.host, args.port)
    url = f"http://{args.host}:{port}"

    if port != args.port:
        print(f"Port {args.port} was not available, so the portal is on {port} instead.")
    print(f"Portal on {url}  (Ctrl+C to stop)")

    if args.open and not args.reload:
        # A short delay so the browser lands on a server that is already answering, opened
        # from a background thread so it never blocks uvicorn's startup.
        threading.Timer(2.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        "ght.api:create_app",
        host=args.host,
        port=port,
        reload=args.reload,
        factory=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
