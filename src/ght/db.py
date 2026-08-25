"""Engine and session factory."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ght.config import settings
from ght.models import Base

_url = settings.database_url
if _url.startswith("sqlite"):
    # Make sure the directory for the SQLite file exists before the engine opens it.
    db_path = Path(_url.split("///", 1)[-1])
    db_path.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(_url, future=True)

# How long a writer waits for the lock before giving up. A fetch writes accounts, evidence
# rows and observations in bursts that outlast SQLite's five-second default, which is what
# made the portal fail rather than wait.
BUSY_TIMEOUT_MS = 20_000


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record) -> None:
    """Let the portal read while a fetch writes.

    Two processes share this database by design - the portal is up while a fetch runs in
    its own subprocess - and SQLite's default rollback journal gives a writer an exclusive
    lock on the whole file. So every page load during a fetch was racing a collector that
    holds the lock for seconds at a time, and losing: `/payees` answered 500 with "database
    is locked" while the fetch it was watching ran perfectly.

    WAL is the fix that matters: readers no longer block on the writer at all, so a page
    load during a fetch reads the last committed state instead of failing. The busy timeout
    covers the remaining case - the portal's own small writes queueing behind the
    collector's - by waiting for the lock rather than erroring the moment it is held.

    Only for SQLite; another engine ignores this entirely.
    """
    if not _url.startswith("sqlite"):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        # Durable enough for this: WAL + NORMAL loses nothing on a process crash, only on
        # an OS-level one, and it keeps a fetch's write bursts from fsyncing per statement.
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


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
