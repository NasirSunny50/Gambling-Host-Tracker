"""Read-only web portal over the collected accounts.

Deliberately read-only and server-rendered: this data is evidence, and the fewer ways
there are to edit it from a browser the better. Collection stays the CLI's job.
"""

from __future__ import annotations

from pathlib import Path

TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app():
    """Build the FastAPI app. Imported lazily so the API extra stays optional."""
    from fastapi import FastAPI

    from ght.api.routes import router

    app = FastAPI(title="Gambling Host Tracker", docs_url="/api/docs")
    app.include_router(router)
    return app
