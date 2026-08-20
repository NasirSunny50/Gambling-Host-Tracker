"""Progress reporting from a collection to whatever is watching it.

A collection takes minutes and drives a browser through a dozen steps. Watching a spinner
tells an operator nothing — least of all when it pauses because it is waiting for *them* to
sign in. So the pipeline emits named steps, and the portal turns them into a live checklist.

The transport is deliberately dumb: one JSON object per line on stdout, prefixed with a
marker so it can be picked out of ordinary output. The collection runs as a subprocess, and
a line of text is the one channel that always works across that boundary.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass

MARKER = "@@GHT-PROGRESS@@"

# Phases, in the order they happen. The portal renders them as a checklist, so this is the
# single source of truth for what a run "looks like".
PHASES: tuple[tuple[str, str], ...] = (
    ("signin", "Sign in to the site"),
    ("collect", "Read each payment method"),
    ("store", "Save accounts and evidence"),
)

PHASE_LABELS = dict(PHASES)


@dataclass(frozen=True)
class Update:
    """One progress event: which phase, what is happening, and how far along."""

    phase: str
    message: str
    step: int | None = None
    total: int | None = None

    def as_dict(self) -> dict:
        return {"phase": self.phase, "message": self.message, "step": self.step, "total": self.total}


# What a caller passes in to receive updates; None means "nobody is watching".
ProgressFn = Callable[[Update], None]


def emit_to_stdout(update: Update) -> None:
    """Write one update as a marked JSON line, flushed so the reader sees it immediately."""
    sys.stdout.write(f"{MARKER}{json.dumps(update.as_dict())}\n")
    sys.stdout.flush()


def parse_line(line: str) -> Update | None:
    """Recover an update from a line of subprocess output, or None if it is ordinary text."""
    if MARKER not in line:
        return None
    try:
        payload = json.loads(line.split(MARKER, 1)[1].strip())
    except ValueError:
        return None
    if not isinstance(payload, dict) or "phase" not in payload:
        return None
    return Update(
        phase=str(payload.get("phase", "")),
        message=str(payload.get("message", "")),
        step=payload.get("step"),
        total=payload.get("total"),
    )


def report(on_progress: ProgressFn | None, phase: str, message: str, **kw) -> None:
    """Send an update if anyone is listening. Convenience so callers need no None checks."""
    if on_progress is not None:
        on_progress(Update(phase=phase, message=message, **kw))
