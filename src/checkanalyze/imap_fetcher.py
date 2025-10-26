"""IMAP polling worker that imports receipt emails into PostgreSQL."""

from __future__ import annotations

import email
import imaplib
import logging
import os
import sys
from collections.abc import Iterable
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from uuid import uuid4

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import Json

load_dotenv()

LOGGER = logging.getLogger("checkanalyze.imap_fetcher")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

SUPPORTED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


class Config:
    """Runtime configuration resolved from environment variables."""

    def __init__(self) -> None:
        self.db_host = _require_env("DB_HOST")
        self.db_port = int(os.getenv("DB_PORT", "5432"))
        self.db_name = _require_env("DB_NAME")
        self.db_user = _require_env("DB_USER")
        self.db_password = _require_env("DB_PASSWORD")
        self.receipts_dir = Path(_require_env("RECEIPTS_DIR"))
        self.imap_host = _require_env("IMAP_HOST")
        self.imap_port = int(os.getenv("IMAP_PORT", "993"))
        self.imap_use_ssl = os.getenv("IMAP_USE_SSL", "true").lower() in {"1", "true", "yes"}
        self.imap_username = _require_env("IMAP_USERNAME")
        self.imap_password = _require_env("IMAP_PASSWORD")
        self.imap_mailbox = os.getenv("IMAP_MAILBOX", "INBOX")
        self.default_user_id = _optional_int(os.getenv("DEFAULT_USER_ID"))

        self.receipts_dir.mkdir(parents=True, exist_ok=True)

    def db_dsn(self) -> str:
        return (
            f"dbname={self.db_name} user={self.db_user} "
            f"password={self.db_password} host={self.db_host} port={self.db_port}"
        )


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigError(f"Environment variable {name} is required")
    return value


def _optional_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        LOGGER.warning("invalid_default_user_id", value=value)
        return None


def main() -> None:
    """Entry point for the IMAP poller."""
    try:
        config = Config()
    except ConfigError as exc:
        LOGGER.error("configuration_error", error=str(exc))
        return

    try:
        conn = psycopg2.connect(config.db_dsn())
    except psycopg2.Error as exc:
        LOGGER.error("database_connection_failed", error=str(exc))
        return

    with conn:
        try:
            _ensure_email_inbox(conn)
        except psycopg2.Error as exc:
            LOGGER.error("database_check_failed", error=str(exc))
            return

    try:
        if config.imap_use_ssl:
            imap: imaplib.IMAP4 = imaplib.IMAP4_SSL(config.imap_host, config.imap_port)
        else:
            imap = imaplib.IMAP4(config.imap_host, config.imap_port)
    except imaplib.IMAP4.error as exc:
        LOGGER.error("imap_connection_failed", error=str(exc))
        conn.close()
        return

    try:
        imap.login(config.imap_username, config.imap_password)
    except imaplib.IMAP4.error as exc:
        LOGGER.error("imap_login_failed", error=str(exc))
        imap.logout()
        conn.close()
        return

    try:
        imap.select(config.imap_mailbox)
    except imaplib.IMAP4.error as exc:
        LOGGER.error("imap_select_failed", mailbox=config.imap_mailbox, error=str(exc))
        imap.logout()
        conn.close()
        return

    try:
        status, data = imap.search(None, "UNSEEN")
        if status != "OK":
            LOGGER.warning("imap_search_failed", status=status)
            return
        message_numbers = data[0].split()
        LOGGER.info("imap_messages_found", count=len(message_numbers))
        for num in message_numbers:
            try:
                _process_message(imap, conn, config, num)
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception(
                    "message_processing_failed", message_num=num.decode(), error=str(exc)
                )
    finally:
        try:
            imap.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass
        conn.close()


def _ensure_email_inbox(conn: psycopg2.extensions.connection) -> None:
    """Ensure email_inbox table exists to prevent runtime crashes."""
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS email_inbox (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                message_id TEXT UNIQUE,
                from_email TEXT,
                subject TEXT,
                received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                processed_at TIMESTAMPTZ
            )
            """
        )
    conn.commit()


def _process_message(
    imap: imaplib.IMAP4,
    conn: psycopg2.extensions.connection,
    config: Config,
    message_number: bytes,
) -> None:
    status, data = imap.fetch(message_number, "(RFC822)")
    if status != "OK":
        LOGGER.warning("imap_fetch_failed", status=status, num=message_number.decode())
        return

    raw_email = data[0][1]
    message = email.message_from_bytes(raw_email)
    ingest_email_message(conn, config, message)
    imap.store(message_number, "+FLAGS", "(\\Seen)")


def ingest_email_message(
    conn: psycopg2.extensions.connection,
    config: Config,
    message: Message,
) -> bool:
    message_id = (message.get("Message-ID") or "").strip()
    from_email = parseaddr(message.get("From", ""))[1] or None
    subject = message.get("Subject")
    received_at = _parse_date(message)

    if _message_exists(conn, message_id):
        LOGGER.info("message_skipped_duplicate", message_id=message_id or "<empty>")
        return False

    user_id = _resolve_user_id(conn, from_email, config.default_user_id)
    if user_id is None:
        LOGGER.warning(
            "user_not_found", from_email=from_email or "", default_user_id=config.default_user_id
        )
        return False

    attachments = list(_extract_attachments(message, config.receipts_dir))
    raw_text = _collect_text(message)

    with conn:
        email_inbox_id = _insert_email_record(
            conn,
            user_id=user_id,
            message_id=message_id or None,
            from_email=from_email,
            subject=subject,
            received_at=received_at,
        )

        if attachments:
            for attachment in attachments:
                _insert_receipt(
                    conn,
                    user_id=user_id,
                    source="email",
                    original_file=str(attachment),
                    raw_text=raw_text,
                    email_inbox_id=email_inbox_id,
                )
        else:
            _insert_receipt(
                conn,
                user_id=user_id,
                source="email",
                original_file=None,
                raw_text=raw_text,
                email_inbox_id=email_inbox_id,
            )

        _mark_processed(conn, email_inbox_id)

    LOGGER.info(
        "message_processed",
        message_id=message_id or "",
        user_id=user_id,
        attachments=len(attachments),
    )
    return True


def _message_exists(conn: psycopg2.extensions.connection, message_id: str) -> bool:
    if not message_id:
        return False
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM email_inbox WHERE message_id = %s", (message_id,))
        return cur.fetchone() is not None


def _resolve_user_id(
    conn: psycopg2.extensions.connection,
    from_email: str | None,
    default_user_id: int | None,
) -> int | None:
    with conn.cursor() as cur:
        if from_email:
            cur.execute(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'users' AND column_name = 'email'
                """
            )
            has_email_column = cur.fetchone() is not None
            if has_email_column:
                cur.execute(
                    "SELECT id FROM users WHERE lower(email) = lower(%s) LIMIT 1",
                    (from_email,),
                )
                row = cur.fetchone()
                if row:
                    return row[0]

        if default_user_id is not None:
            cur.execute("SELECT id FROM users WHERE id = %s", (default_user_id,))
            row = cur.fetchone()
            if row:
                return row[0]

        cur.execute("SELECT id FROM users ORDER BY id ASC LIMIT 1")
        row = cur.fetchone()
        if row:
            return row[0]
    return None


def _extract_attachments(message: Message, target_dir: Path) -> Iterable[Path]:
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if not filename:
            continue
        extension = Path(filename).suffix.lower()
        if extension not in SUPPORTED_EXTENSIONS:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        safe_name = f"{uuid4().hex}{extension}"
        file_path = target_dir / safe_name
        file_path.write_bytes(payload)
        yield file_path


def _collect_text(message: Message) -> str | None:
    texts: list[str] = []
    for part in message.walk():
        if part.get_content_type() == "text/plain" and part.get_content_disposition() in (
            None,
            "inline",
        ):
            payload = part.get_payload(decode=True)
            if payload is not None:
                try:
                    texts.append(
                        payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    )
                except LookupError:
                    texts.append(payload.decode("utf-8", errors="replace"))
    if texts:
        return "\n\n".join(texts)
    return None


def _insert_email_record(
    conn: psycopg2.extensions.connection,
    *,
    user_id: int,
    message_id: str | None,
    from_email: str | None,
    subject: str | None,
    received_at: object | None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO email_inbox (user_id, message_id, from_email, subject, received_at)
            VALUES (%s, %s, %s, %s, COALESCE(%s, now()))
            RETURNING id
            """,
            (user_id, message_id, from_email, subject, received_at),
        )
        row = cur.fetchone()
        assert row is not None
        return row[0]


def _insert_receipt(
    conn: psycopg2.extensions.connection,
    *,
    user_id: int,
    source: str,
    original_file: str | None,
    raw_text: str | None,
    email_inbox_id: int,
) -> None:
    payload = {
        "email_inbox_id": email_inbox_id,
    }
    if original_file:
        payload.setdefault("attachments", []).append(original_file)
    if raw_text:
        payload["raw_text_length"] = len(raw_text)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO receipts (user_id, source, original_file, raw_text, json_payload, status)
            VALUES (%s, %s, %s, %s, %s, 'pending')
            """,
            (user_id, source, original_file, raw_text, Json(payload)),
        )


def _mark_processed(conn: psycopg2.extensions.connection, email_inbox_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE email_inbox SET processed_at = now() WHERE id = %s",
            (email_inbox_id,),
        )


def _parse_date(message: Message) -> object | None:
    date_header = message.get("Date")
    if not date_header:
        return None
    try:
        dt = parsedate_to_datetime(date_header)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception:  # noqa: BLE001
        LOGGER.exception("fatal_error")
        sys.exit(0)
