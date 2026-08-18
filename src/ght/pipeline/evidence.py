"""Evidence storage.

Whatever the server returned is written to disk verbatim and addressed by the SHA-256 of
its own bytes. Two properties matter for an AML case file:

* the blob can be re-hashed at any time to prove it is the same content the number was
  extracted from, and
* identical pages across runs collapse onto one file, so keeping months of three-a-day
  captures stays cheap while every run still gets its own evidence row.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from ght.config import settings
from ght.types import RawCapture

EXTENSIONS = {"html": "html", "json": "json", "screenshot": "png"}


@dataclass(frozen=True)
class StoredBlob:
    kind: str
    path: str  # relative to the evidence directory
    sha256: str
    bytes: int


def store_blob(slug: str, kind: str, data: bytes, root: Path | None = None) -> StoredBlob:
    """Write ``data`` under its own hash and return the record to persist."""
    digest = hashlib.sha256(data).hexdigest()
    extension = EXTENSIONS.get(kind, "bin")
    # Shard by the first two hex characters so no directory grows unbounded.
    relative = Path(slug) / digest[:2] / f"{digest}.{extension}"

    destination = (root or settings.evidence_dir) / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        destination.write_bytes(data)

    return StoredBlob(
        kind=kind, path=str(relative).replace("\\", "/"), sha256=digest, bytes=len(data)
    )


def store_capture(slug: str, capture: RawCapture, root: Path | None = None) -> list[StoredBlob]:
    """Store every artefact a fetcher produced for one page."""
    blobs: list[StoredBlob] = []
    if capture.html:
        blobs.append(store_blob(slug, "html", capture.html.encode("utf-8"), root))
    if capture.json_body:
        blobs.append(store_blob(slug, "json", capture.json_body.encode("utf-8"), root))
    if capture.screenshot:
        blobs.append(store_blob(slug, "screenshot", capture.screenshot, root))
    return blobs


def verify(blob_path: str, expected_sha256: str, root: Path | None = None) -> bool:
    """Re-hash a stored blob and confirm it still matches what was recorded."""
    path = (root or settings.evidence_dir) / blob_path
    if not path.exists():
        return False
    return hashlib.sha256(path.read_bytes()).hexdigest() == expected_sha256
