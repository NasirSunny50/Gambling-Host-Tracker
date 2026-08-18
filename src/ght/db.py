"""Engine and session factory."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from ght.config import settings
from ght.models import Base

_url = settings.database_url
if _url.startswith("sqlite"):
    # Make sure the directory for the SQLite file exists before the engine opens it.
    db_path = Path(_url.split("///", 1)[-1])
    db_path.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(_url, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def create_all() -> None:
    """Create tables directly. Alembic owns schema changes once migrations exist."""
    Base.metadata.create_all(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
