"""Where a site's sign-in credentials come from.

Only the environment (and the gitignored ``.env`` it is loaded from). Never a YAML file,
never the database, never an argument that could end up in a log line or a run report: the
source configs live in git, and a password that reaches one is a password that has to be
rotated.

Naming follows the site slug, so adding a site adds two variables and no code:

    GHT_LOGIN_1XBET_BD_USERNAME=...
    GHT_LOGIN_1XBET_BD_PASSWORD=...

Absent credentials are not an error. They mean the run signs in the way it always has —
by opening a window for the operator — so a machine that has none is exactly as capable
as it was before, just less automatic.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

PREFIX = "GHT_LOGIN"


@dataclass(frozen=True)
class Credentials:
    """A username/password pair for one site. Never logged, never persisted."""

    username: str = ""
    password: str = ""

    def __bool__(self) -> bool:
        """Truthy only when both halves are present — half a pair cannot sign anything in."""
        return bool(self.username and self.password)

    def __repr__(self) -> str:  # pragma: no cover - defensive, but cheap
        """Redacted on purpose: these objects travel through exception handlers."""
        return f"Credentials(username={'set' if self.username else 'unset'}, password=***)"


def env_names(slug: str) -> tuple[str, str]:
    """The two variable names a slug maps to."""
    key = re.sub(r"[^A-Za-z0-9]+", "_", slug).strip("_").upper()
    return f"{PREFIX}_{key}_USERNAME", f"{PREFIX}_{key}_PASSWORD"


def _environment() -> dict[str, str]:
    """The real environment, with ``.env`` behind it.

    Settings reads ``.env`` through pydantic, which never puts the values into
    ``os.environ`` — so reading the process environment alone would silently ignore the
    file the operator actually edited. A real variable still wins over the file.
    """
    values: dict[str, str] = {}
    try:
        from dotenv import dotenv_values

        from ght.config import REPO_ROOT

        values = {k: v for k, v in dotenv_values(REPO_ROOT / ".env").items() if v is not None}
    except Exception:  # noqa: BLE001 - no .env, or no dotenv; the environment still counts
        values = {}
    values.update(os.environ)
    return values


def for_site(slug: str, env: dict[str, str] | None = None) -> Credentials:
    """Credentials for one site, or an empty pair when none are configured."""
    source = _environment() if env is None else env
    user_var, password_var = env_names(slug)
    return Credentials(
        username=(source.get(user_var) or "").strip(),
        password=(source.get(password_var) or "").strip(),
    )
