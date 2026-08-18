"""End-to-end demo of the multi-step deposit flow, against a local mock site.

The real targets need a login and are not reachable from a dev box, so this serves
tests/fixtures/mocksite/ — which mirrors the shape of a paykassma-brokered deposit flow —
and drives the whole pipeline over it: browser fetch, click flow, extraction, evidence.

    python scripts/demo_flow.py           # happy path
    python scripts/demo_flow.py --break   # same run with the Confirm button renamed

Needs the browser extra: pip install -e ".[browser]" && playwright install chromium
"""

from __future__ import annotations

import argparse
import functools
import http.server
import socketserver
import sys
import tempfile
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

def _bootstrap() -> None:
    """Make the script runnable as `python scripts/...` from a plain checkout.

    Two things go wrong here. The package may simply not be on sys.path, which adding src/
    fixes. Or it is the wrong interpreter - the system Python instead of the project venv -
    and no path fiddling helps, because the dependencies are not installed there either.
    Telling those apart in the message saves the reader a confusing traceback.
    """
    src = REPO_ROOT / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))
    try:
        # Importing the package alone proves nothing: src/ is now on sys.path, so it
        # succeeds even where none of the dependencies exist. Import a module that
        # actually pulls them in.
        from ght import sources  # noqa: F401
    except ImportError:
        venv = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
        script = Path(__file__).relative_to(REPO_ROOT)
        hint = f"  {venv.relative_to(REPO_ROOT)} {script}" if venv.exists() else '  pip install -e ".[browser]"'
        sys.exit(
            f"""Cannot import 'ght' with {Path(sys.executable).name} ({sys.executable}).
This project's dependencies live in its own virtualenv. Run it with:
{hint}"""
        )

MOCKSITE = REPO_ROOT / "tests" / "fixtures" / "mocksite"


def serve(directory: Path) -> tuple[str, socketserver.TCPServer]:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}/index.html", httpd


def main() -> None:
    _bootstrap()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--break",
        dest="break_flow",
        action="store_true",
        help="rename the Confirm button, as a site redesign would",
    )
    args = parser.parse_args()

    # Point evidence at a throwaway dir so a demo never writes into the real store.
    from ght.config import settings

    settings.evidence_dir = Path(tempfile.mkdtemp()) / "evidence"

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from ght.models import Alert, Base
    from ght.pipeline.run import run_site
    from ght.sources import Block, SourceConfig, SourceUrl, Step

    url, httpd = serve(MOCKSITE)
    try:
        # Built here rather than loaded from sources/, so the demo keeps working whatever
        # the real site configs are doing this week.
        confirm = "#deposit_button_v2" if args.break_flow else "#deposit_button"
        config = SourceConfig(
            slug="mocksite",
            name="Mock deposit flow",
            status="active",
            fetcher="browser",
            urls=[SourceUrl(url=url)],
            flow=[
                Step(click='text="Make a deposit"', wait_for=".payment-cell"),
                Step(click='.payment-cell[data-method="nagad_b_webdef"]', wait_for="#deposit_button"),
                Step(click=confirm, wait_for=".merchant-name"),
            ],
            wait_for=".merchant-name",
            blocks=[
                Block(
                    channel="nagad",
                    container=".psp-panel",
                    value=".account-number",
                    holder=".merchant-name",
                )
            ],
        )

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            report = run_site(session, config)
            session.commit()

            print(f"status     : {report.status}")
            print(f"landed on  : {report.url}")
            print(f"flow_error : {report.flow_error or '-'}")
            for account in report.extraction.accounts if report.extraction else []:
                print(
                    f"account    : {account.channel} {account.account_number} "
                    f"holder={account.holder_name!r} confidence={account.confidence}"
                )
            print(f"evidence   : {len(report.evidence_paths)} blob(s)")
            print(f"alerts     : {[a.type for a in session.scalars(select(Alert))]}")
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    main()
