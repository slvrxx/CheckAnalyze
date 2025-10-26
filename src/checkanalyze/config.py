"""Configuration management for CheckAnalyze services."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import quote_plus

from dotenv import load_dotenv

load_dotenv()


def _env(key: str, default: str | None = None) -> str | None:
    value = os.getenv(key)
    if value is None:
        return default
    return value


def _env_int(key: str) -> int | None:
    value = _env(key)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _env_bool(key: str, default: bool) -> bool:
    value = _env(key)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes"}


def _env_str(key: str, default: str = "") -> str:
    return cast(str, _env(key, default))


@dataclass(slots=True)
class DatabaseConfig:
    """Settings for connecting to PostgreSQL."""

    host: str
    port: int
    name: str
    user: str
    password: str
    url: str

    def __init__(self) -> None:
        default_host = _env_str("DB_HOST", "localhost")
        default_port = int(_env_str("DB_PORT", "5432"))
        default_name = _env_str("DB_NAME", "checkanalyze")
        default_user = _env_str("DB_USER", "checkanalyze")
        default_password = _env_str("DB_PASSWORD", "")

        database_url = _env("DATABASE_URL")
        if database_url:
            self.url = database_url
            self.host = default_host
            self.port = default_port
            self.name = default_name
            self.user = default_user
            self.password = default_password
        else:
            quoted_user = quote_plus(default_user)
            quoted_password = quote_plus(default_password)
            self.url = f"postgresql+psycopg2://{quoted_user}:{quoted_password}@{default_host}:{default_port}/{default_name}"
            self.host = default_host
            self.port = default_port
            self.name = default_name
            self.user = default_user
            self.password = default_password


@dataclass(slots=True)
class TelegramConfig:
    """Telegram bot configuration."""

    token: str = _env_str("TELEGRAM_BOT_TOKEN", "")
    admin_id: int | None = _env_int("TELEGRAM_ADMIN_ID")


@dataclass(slots=True)
class EmailConfig:
    """IMAP mailbox configuration."""

    host: str = _env_str("IMAP_HOST", "")
    port: int = int(_env_str("IMAP_PORT", "993"))
    use_ssl: bool = _env_bool("IMAP_USE_SSL", True)
    username: str = _env_str("IMAP_USERNAME", "")
    password: str = _env_str("IMAP_PASSWORD", "")
    mailbox: str = _env_str("IMAP_MAILBOX", "INBOX")


@dataclass(slots=True)
class AppConfig:
    """Top-level configuration container."""

    database: DatabaseConfig = DatabaseConfig()
    telegram: TelegramConfig = TelegramConfig()
    email: EmailConfig = EmailConfig()
    receipts_dir: Path = Path(_env_str("RECEIPTS_DIR", "/tmp/checkanalyze_receipts"))
    default_user_id: int | None = _env_int("DEFAULT_USER_ID")


CONFIG = AppConfig()

__all__ = [
    "CONFIG",
    "AppConfig",
    "DatabaseConfig",
    "TelegramConfig",
    "EmailConfig",
]
