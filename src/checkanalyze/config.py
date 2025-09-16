"""Configuration management for CheckAnalyze services."""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


def _env(key: str, default: Optional[str] = None) -> Optional[str]:
    """Read an environment variable."""
    value = os.getenv(key)
    if value is None:
        return default
    return value


@dataclass(slots=True)
class DatabaseConfig:
    """Settings for connecting to PostgreSQL."""

    url: str = _env("DATABASE_URL", "postgresql+psycopg2://user:pass@localhost:5432/checkanalyze")


@dataclass(slots=True)
class TelegramConfig:
    """Telegram bot configuration."""

    token: str = _env("TELEGRAM_BOT_TOKEN", "")
    allowed_chat_id: Optional[int] = (
        int(_env("TELEGRAM_ALLOWED_CHAT_ID")) if _env("TELEGRAM_ALLOWED_CHAT_ID") else None
    )
    webhook_url: Optional[str] = _env("TELEGRAM_WEBHOOK_URL")


@dataclass(slots=True)
class EmailConfig:
    """IMAP mailbox configuration."""

    host: str = _env("IMAP_HOST", "")
    username: str = _env("IMAP_USERNAME", "")
    password: str = _env("IMAP_PASSWORD", "")
    mailbox: str = _env("IMAP_MAILBOX", "INBOX")
    check_interval: int = int(_env("IMAP_CHECK_INTERVAL", "120"))
    use_ssl: bool = _env("IMAP_USE_SSL", "1") == "1"


@dataclass(slots=True)
class AppConfig:
    """Top-level configuration container."""

    database: DatabaseConfig = DatabaseConfig()
    telegram: TelegramConfig = TelegramConfig()
    email: EmailConfig = EmailConfig()
    default_user_id: Optional[int] = (
        int(_env("DEFAULT_USER_ID")) if _env("DEFAULT_USER_ID") else None
    )


CONFIG = AppConfig()

__all__ = ["CONFIG", "AppConfig", "DatabaseConfig", "TelegramConfig", "EmailConfig"]
