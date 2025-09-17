"""Integration tests using PostgreSQL for migrations and email ingestion."""

from __future__ import annotations

from collections.abc import Iterator
from email.message import EmailMessage
from pathlib import Path

import psycopg2
import pytest

try:
    from pytest_postgresql.factories import init_postgresql_database
except Exception:  # noqa: BLE001
    init_postgresql_database = None

from checkanalyze.imap_fetcher import Config, ingest_email_message

MIGRATION_PATH = Path("migrations/0001_prod_schema.sql")


postgresql = init_postgresql_database() if callable(init_postgresql_database) else None


@pytest.fixture()
def pg_connection() -> Iterator[psycopg2.extensions.connection]:
    if postgresql is None:
        pytest.skip("PostgreSQL binaries are not available")
    try:
        dsn = postgresql.dsn()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL fixture unavailable: {exc}")
        return
    conn = psycopg2.connect(**dsn)
    conn.autocommit = True
    _prepare_base_schema(conn)
    yield conn
    conn.close()


def _prepare_base_schema(conn: psycopg2.extensions.connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                email TEXT,
                telegram_id BIGINT,
                name TEXT
            );

            CREATE TABLE IF NOT EXISTS categories (
                id BIGSERIAL PRIMARY KEY,
                code VARCHAR(32),
                name TEXT
            );

            INSERT INTO categories (id, code, name)
            VALUES (1, 'default', 'Default Category')
            ON CONFLICT (id) DO NOTHING;

            CREATE TABLE IF NOT EXISTS transactions (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(id),
                category_id BIGINT NOT NULL REFERENCES categories(id),
                amount NUMERIC(14,2),
                currency VARCHAR(10),
                is_approximate BOOLEAN DEFAULT FALSE,
                comment TEXT,
                created_at TIMESTAMPTZ DEFAULT now()
            );
            """
        )
        cur.execute(
            """
            INSERT INTO users (id, email, name)
            VALUES (1, 'user@example.com', 'Tester')
            ON CONFLICT (id) DO NOTHING
            """
        )


def test_migration_is_idempotent(pg_connection) -> None:
    script = MIGRATION_PATH.read_text()
    with pg_connection.cursor() as cur:
        cur.execute(script)
        cur.execute(script)

    with pg_connection.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM receipts")
        count = cur.fetchone()
        assert count is not None


def test_ingest_email_creates_receipt(
    pg_connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = MIGRATION_PATH.read_text()
    with pg_connection.cursor() as cur:
        cur.execute(script)

    receipts_dir = tmp_path / "receipts"
    monkeypatch.setenv("RECEIPTS_DIR", str(receipts_dir))
    monkeypatch.setenv("DB_HOST", pg_connection.info.host)
    monkeypatch.setenv("DB_PORT", str(pg_connection.info.port))
    monkeypatch.setenv("DB_NAME", pg_connection.info.dbname)
    monkeypatch.setenv("DB_USER", pg_connection.info.user)
    monkeypatch.setenv("DB_PASSWORD", pg_connection.info.password or "")
    monkeypatch.setenv("IMAP_HOST", "imap.test")
    monkeypatch.setenv("IMAP_PORT", "993")
    monkeypatch.setenv("IMAP_USE_SSL", "true")
    monkeypatch.setenv("IMAP_USERNAME", "tester")
    monkeypatch.setenv("IMAP_PASSWORD", "secret")
    monkeypatch.setenv("IMAP_MAILBOX", "INBOX")
    monkeypatch.setenv("DEFAULT_USER_ID", "1")

    config = Config()

    message = EmailMessage()
    message["Subject"] = "Test Receipt"
    message["From"] = "Tester <user@example.com>"
    message["To"] = "bot@example.com"
    message["Message-ID"] = "<test-1@example.com>"
    message.set_content("Спасибо за покупку")
    message.add_attachment(
        b"PDFDATA",
        maintype="application",
        subtype="pdf",
        filename="receipt.pdf",
    )

    ingested = ingest_email_message(pg_connection, config, message)
    assert ingested is True

    with pg_connection.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM email_inbox")
        inbox_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM receipts")
        receipt_count = cur.fetchone()[0]
        cur.execute("SELECT original_file, status, source FROM receipts")
        file_path, status, source = cur.fetchone()

    assert inbox_count == 1
    assert receipt_count == 1
    assert status == "pending"
    assert source == "email"
    assert Path(file_path).exists()

    # Duplicate message should be ignored
    ingested_again = ingest_email_message(pg_connection, config, message)
    assert ingested_again is False

    with pg_connection.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM receipts")
        assert cur.fetchone()[0] == 1

    # Clean up saved files
    for path in receipts_dir.glob("*"):
        path.unlink()
