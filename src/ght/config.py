"""Runtime configuration, loaded from environment / .env."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = "sqlite:///./data/ght.db"
    evidence_dir: Path = REPO_ROOT / "data" / "evidence"
    sources_dir: Path = REPO_ROOT / "sources"

    request_timeout: int = 30
    max_retries: int = 3
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    # Fernet key for encrypting stored site credentials. Empty disables the feature
    # (credentials cannot be stored or used) rather than falling back to plaintext.
    # Read from GHT_SECRET_KEY specifically, so it stands out among generic env names.
    secret_key: str = Field("", validation_alias="GHT_SECRET_KEY")

    slack_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


settings = Settings()
