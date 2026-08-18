"""Collect deposit accounts from 1xBet BD.

The deposit page is behind a login, so this runs in two phases:

    python scripts/collect_1xbet.py --login     # once, opens a real browser for you
    python scripts/collect_1xbet.py             # every time after that, unattended

The login phase opens a visible browser and waits while YOU sign in. It never asks for,
stores, or types a password — when you are done it saves only the resulting session
cookies to data/auth/1xbet-bd.json, and the collection phase reuses those. Re-run --login
whenever the session expires.

Everything after that is automatic: the collector walks the deposit flow defined in
sources/1xbet-bd.yaml (pick the method tile, confirm, follow the redirect), captures the
page and a screenshot as evidence, extracts the payee, and records what changed since the
last run.

Needs the browser extra:  pip install -e ".[browser]" && playwright install chromium
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
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

SLUG = "1xbet-bd"
AUTH_PATH = REPO_ROOT / "data" / "auth" / f"{SLUG}.json"


# Playwright's own chromium is the first choice, but security software on Windows often
# refuses to spawn it (it is unsigned and launches from a temp profile), which surfaces as
# a bare "spawn UNKNOWN". The browsers already installed on the machine are trusted, and a
# session captured in any of them works in all of them, so fall through to those.
HEADED_CHANNELS = (None, "msedge", "chrome")


def launch_headed(playwright, channel: str | None):
    """Open a visible browser, trying the installed ones if the bundled one is blocked."""
    channels = (channel,) if channel else HEADED_CHANNELS
    failures = []
    for candidate in channels:
        try:
            kwargs = {"channel": candidate} if candidate else {}
            browser = playwright.chromium.launch(headless=False, **kwargs)
            return browser, candidate or "bundled chromium"
        except Exception as exc:  # noqa: BLE001 - trying the next browser is the point
            failures.append(f"  {candidate or 'bundled chromium'}: {str(exc).splitlines()[0]}")

    print("Could not open a visible browser. Tried:")
    for failure in failures:
        print(failure)
    print()
    print("Fixes, easiest first:")
    print("  - install Chrome or Edge, then re-run")
    print('  - re-download the bundled browser:  python -m playwright install chromium')
    print("  - if antivirus is blocking it, allow the ms-playwright folder")
    return None, None


def capture_session(start_url: str, channel: str | None = None) -> int:
    """Open a browser, let the user sign in, and save the session cookies."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('playwright missing: pip install -e ".[browser]" && playwright install chromium')
        return 1

    AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)

    print(f"Opening {start_url}")
    print("Sign in yourself in the browser window, then come back here and press Enter.")
    print("This script does not read or type your credentials.\n")

    with sync_playwright() as playwright:
        browser, used = launch_headed(playwright, channel)
        if browser is None:
            return 1
        used_channel = None if used == "bundled chromium" else used
        print(f"(using {used})")
        print()
        context = browser.new_context(locale="en-US", viewport={"width": 1366, "height": 900})
        page = context.new_page()
        page.goto(start_url, wait_until="domcontentloaded")

        try:
            input("Press Enter once you are logged in (Ctrl+C to abort)... ")
        except KeyboardInterrupt:
            print("\naborted, nothing saved")
            browser.close()
            return 1

        # Save the browser identity alongside the cookies. Replaying them under a
        # different User-Agent invalidates Cloudflare's clearance cookie and the site
        # treats the run as a new device, which looks exactly like never having logged in.
        state = context.storage_state()
        user_agent = page.evaluate("navigator.userAgent")
        browser.close()

    AUTH_PATH.write_text(
        json.dumps(
            {"storage_state": state, "user_agent": user_agent, "channel": used_channel},
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"\nsession saved to {AUTH_PATH}")
    print("Keep this file private: it grants access to the logged-in account.")
    print(f"Now run:  python {Path(__file__).relative_to(REPO_ROOT)}")
    return 0


def session_age_hours() -> float | None:
    if not AUTH_PATH.exists():
        return None
    mtime = datetime.fromtimestamp(AUTH_PATH.stat().st_mtime, tz=UTC)
    return (datetime.now(UTC) - mtime).total_seconds() / 3600


def collect(dry_run: bool, channel: str | None = None) -> int:
    from ght.db import create_all, session_scope
    from ght.pipeline.run import run_site
    from ght.sources import load_source

    config = load_source(SLUG)
    if channel:
        config = config.model_copy(update={"browser_channel": channel})

    age = session_age_hours()
    if age is None:
        print(f"No saved session at {AUTH_PATH}.")
        print("The run will proceed logged out and will almost certainly see nothing.")
        print(f"Run this first:  python {Path(__file__).relative_to(REPO_ROOT)} --login\n")
    else:
        print(f"Using session saved {age:.1f}h ago\n")

    if config.status != "active":
        # Draft configs carry unverified selectors; say so rather than reporting an empty
        # result as if the site had published nothing.
        print(f"NOTE: {SLUG} is status '{config.status}' - its selectors are not verified yet.")
        print("A zero-account result here means the config needs work, not that the site")
        print("has no accounts.\n")

    create_all()
    with session_scope() as session:
        report = run_site(session, config, dry_run=dry_run)

        print(f"status     : {report.status}")
        print(f"landed on  : {report.url or '-'}")
        if report.error:
            print(f"error      : {report.error}")
        if report.flow_error:
            print(f"flow issue : {report.flow_error}")

        accounts = report.extraction.accounts if report.extraction else []
        for account in accounts:
            holder = f" holder={account.holder_name}" if account.holder_name else ""
            print(
                f"account    : {account.channel} {account.account_number}{holder} "
                f"confidence={account.confidence}"
            )
        for name in report.merchants:
            print(f"merchant   : {name} (name only, no receiving number shown)")
        if not accounts and not report.merchants:
            print("account    : none found")

        print(f"evidence   : {len(report.evidence_paths)} blob(s)")
        print(f"new        : {len(report.changes.new_account_ids)}")
        print(f"disappeared: {len(report.changes.disappeared_account_ids)}")

        if report.extraction and report.extraction.extractor_looks_broken:
            print("\nWARNING: the page contains numbers the configured selectors missed.")
            print("The selectors are probably stale — check the saved evidence HTML.")

    return 0 if report.status == "ok" else 1


def main() -> int:
    _bootstrap()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--login",
        action="store_true",
        help="open a browser so you can sign in, and save the session",
    )
    parser.add_argument(
        "--url",
        default="https://bd.1xbet.com/",
        help="page to open for the login step",
    )
    parser.add_argument(
        "--channel",
        default=None,
        help="force a browser: msedge, chrome, or omit to try each in turn",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="extract and print, write nothing to the DB"
    )
    args = parser.parse_args()

    if args.login:
        return capture_session(args.url, args.channel)
    return collect(args.dry_run, args.channel)


if __name__ == "__main__":
    sys.exit(main())
