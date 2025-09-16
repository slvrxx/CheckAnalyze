"""IMAP email ingestion for receipt processing."""
from __future__ import annotations

import email
import imaplib
import time
from email.message import Message
from typing import Iterable, Optional

import structlog

from .config import CONFIG
from .db import EmailIdentity, session_scope
from .service import process_receipt_bytes

logger = structlog.get_logger(__name__)


class EmailIngestor:
    """Poll an IMAP mailbox and ingest PDF attachments as receipts."""

    def __init__(self) -> None:
        self.config = CONFIG.email
        if not self.config.host:
            logger.warning("imap_disabled", reason="host_missing")
        init_missing_tables()

    def run_forever(self) -> None:
        if not self.config.host:
            logger.error("imap_not_configured")
            return
        logger.info("imap_ingestor_started", host=self.config.host, mailbox=self.config.mailbox)
        while True:
            try:
                processed = self.poll_once()
                if processed:
                    logger.info("imap_cycle", processed=processed)
            except Exception as exc:
                logger.exception("imap_error", error=str(exc))
            time.sleep(self.config.check_interval)

    def poll_once(self) -> int:
        if not self.config.host:
            return 0

        if self.config.use_ssl:
            client: imaplib.IMAP4 = imaplib.IMAP4_SSL(self.config.host)
        else:
            client = imaplib.IMAP4(self.config.host)
        try:
            client.login(self.config.username, self.config.password)
            client.select(self.config.mailbox)
            status, data = client.search(None, "UNSEEN")
            if status != "OK":
                return 0
            message_ids = data[0].split()
            processed = 0
            for msg_id in message_ids:
                status, msg_data = client.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue
                raw_email = msg_data[0][1]
                message = email.message_from_bytes(raw_email)
                user_id = self._resolve_user(message)
                if not user_id:
                    logger.warning("email_no_user", sender=message.get("from"))
                    continue
                attachments = list(_extract_pdf_attachments(message))
                if not attachments:
                    continue
                for filename, payload in attachments:
                    result = process_receipt_bytes(user_id, payload, source="email")
                    logger.info(
                        "email_receipt_processed",
                        user_id=user_id,
                        filename=filename,
                        receipt_id=result.receipt_id,
                    )
                processed += 1
                client.store(msg_id, "+FLAGS", "(\\Seen)")
            return processed
        finally:
            try:
                client.logout()
            except Exception:
                pass

    def _resolve_user(self, message: Message) -> Optional[int]:
        sender = message.get("from", "")
        address = _extract_address(sender)
        if not address:
            return CONFIG.default_user_id

        with session_scope() as session:
            identity = session.query(EmailIdentity).filter(EmailIdentity.email == address).one_or_none()
            if identity:
                return identity.user_id

        return CONFIG.default_user_id


def _extract_pdf_attachments(message: Message) -> Iterable[tuple[str, bytes]]:
    for part in message.walk():
        content_disposition = part.get("Content-Disposition", "")
        if "attachment" not in content_disposition.lower():
            continue
        filename = part.get_filename()
        if not filename or not filename.lower().endswith(".pdf"):
            continue
        payload = part.get_payload(decode=True)
        if payload:
            yield filename, payload


def _extract_address(header_value: str) -> Optional[str]:
    if not header_value:
        return None
    addresses = email.utils.getaddresses([header_value])
    if not addresses:
        return None
    return addresses[0][1].lower()


def init_missing_tables() -> None:
    """Ensure EmailIdentity table exists."""
    from .db import init_schema

    init_schema()


__all__ = ["EmailIngestor", "init_missing_tables"]
