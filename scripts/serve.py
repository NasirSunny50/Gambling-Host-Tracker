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
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


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
    args = parser.parse_args()

    import uvicorn

    print(f"Portal on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    uvicorn.run(
        "ght.api:create_app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        factory=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
