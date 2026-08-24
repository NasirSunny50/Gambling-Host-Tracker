"""Read-only web portal over the collected accounts.

Deliberately read-only and server-rendered: this data is evidence, and the fewer ways
there are to edit it from a browser the better. Collection stays the CLI's job.
"""

from __future__ import annotations

from pathlib import Path

TEMPLATES_DIR = Path(__file__).parent / "templates"
# The product's own logo, shipped with the package so it serves regardless of the working
# directory. `logo-badge.png` is the full 1024px mark; `favicon.png` a 128px cut for the
# tab, small enough to fetch cheaply before it is cached.
ASSETS_DIR = Path(__file__).parent / "assets"


def create_app():
    """Build the FastAPI app. Imported lazily so the API extra stays optional."""
    from fastapi import FastAPI

    from ght.api.routes import router

    app = FastAPI(title="Gambling Host Tracker", docs_url="/api/docs")
    app.include_router(router)

    # Pick a saved schedule back up. Done here rather than on import so that anything
    # importing the routes - a test, a script - does not quietly start collecting.
    from ght.api.routes import scheduler

    scheduler.resume()
    return app
