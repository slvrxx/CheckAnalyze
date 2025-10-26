"""Business logic helpers for receipts and inbox processing."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import structlog
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from .config import CONFIG
from .db import Category, Receipt, Transaction, session_scope

LOGGER = structlog.get_logger(__name__)


@dataclass(slots=True)
class ReceiptLine:
    line_no: int | None
    item_name: str
    amount: Decimal
    currency: str
    category_id: int | None


@dataclass(slots=True)
class InboxEntry:
    receipt: Receipt
    lines: list[ReceiptLine]


def ensure_receipts_dir() -> Path:
    """Ensure the receipts directory exists and return it."""
    path = CONFIG.receipts_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def store_receipt_file(user_id: int, data: bytes, filename: str, *, source: str) -> Receipt:
    """Persist an uploaded receipt file and create a pending record."""
    receipts_dir = ensure_receipts_dir()
    extension = Path(filename).suffix or ".bin"
    safe_name = f"{uuid4().hex}{extension}"
    file_path = receipts_dir / safe_name
    file_path.write_bytes(data)

    with session_scope() as session:
        receipt = Receipt(
            user_id=user_id,
            source=source,
            original_file=str(file_path),
            status="pending",
        )
        session.add(receipt)
        session.flush()
        session.refresh(receipt)
        LOGGER.info("receipt_stored", receipt_id=receipt.id, user_id=user_id, source=source)
        return receipt


def list_inbox_entries(user_id: int) -> list[InboxEntry]:
    """Fetch pending receipts with their items for the inbox view."""
    with session_scope() as session:
        receipts = (
            session.execute(
                select(Receipt)
                .where(Receipt.user_id == user_id, Receipt.status == "pending")
                .order_by(Receipt.created_at.desc())
            )
            .scalars()
            .all()
        )
        entries: list[InboxEntry] = []
        for receipt in receipts:
            lines = [
                ReceiptLine(
                    line_no=item.line_no,
                    item_name=item.item_name or "Позиция",
                    amount=_as_decimal(item.amount),
                    currency=receipt.currency or "RUB",
                    category_id=item.category_id,
                )
                for item in sorted(receipt.items, key=lambda i: (i.line_no or 0))
            ]
            entries.append(InboxEntry(receipt=receipt, lines=lines))
        return entries


def format_inbox_entry(entry: InboxEntry) -> str:
    """Create a human-readable summary for a pending receipt."""
    receipt = entry.receipt
    header_parts = [f"Чек #{receipt.id}"]
    if receipt.merchant_name:
        header_parts.append(receipt.merchant_name)
    if receipt.total_amount is not None and receipt.currency:
        header_parts.append(f"Сумма: {receipt.total_amount:.2f} {receipt.currency}")
    header = " — ".join(header_parts)

    lines: list[str] = [header]
    if entry.lines:
        for line in entry.lines:
            amount = f"{line.amount:.2f}" if line.amount is not None else "0.00"
            category = str(line.category_id) if line.category_id is not None else "?"
            prefix = f"{line.line_no}." if line.line_no else "-"
            summary = (
                f"{prefix} {line.item_name} — {amount} {line.currency}" f" → категория {category}"
            )
            lines.append(summary)
    else:
        lines.append("Нет позиций. Можно подтвердить общую сумму.")
    return "\n".join(lines)


def confirm_receipt_total(user_id: int, receipt_id: int) -> tuple[str, bool]:
    """Create a single transaction for the total amount."""
    with session_scope() as session:
        receipt = _get_receipt_for_user(session, user_id, receipt_id)
        if receipt is None:
            return ("Чек не найден или нет доступа.", False)
        if receipt.status != "pending":
            return ("Чек уже обработан.", False)
        if receipt.total_amount is None or receipt.currency is None:
            return ("Неизвестна сумма чека — подтвердите позиции вручную.", False)

        category_id = _resolve_total_category(session, receipt)
        if category_id is None:
            return ("Не найдена категория для записи транзакции.", False)

        transaction = Transaction(
            user_id=user_id,
            category_id=category_id,
            amount=receipt.total_amount,
            currency=receipt.currency,
            is_approximate=False,
            comment=f"Receipt {receipt.id}",
            source="email" if receipt.source == "email" else receipt.source,
            receipt_id=receipt.id,
        )
        session.add(transaction)
        receipt.status = "confirmed"
        session.flush()

    _cleanup_receipt_file(receipt)
    return (f"Создана транзакция на сумму {receipt.total_amount:.2f} {receipt.currency}.", True)


def confirm_receipt_items(user_id: int, receipt_id: int) -> tuple[str, bool]:
    """Create transactions for each receipt item."""
    with session_scope() as session:
        receipt = _get_receipt_for_user(session, user_id, receipt_id)
        if receipt is None:
            return ("Чек не найден или нет доступа.", False)
        if receipt.status != "pending":
            return ("Чек уже обработан.", False)
        if not receipt.items:
            return ("У чека нет позиций — подтвердите общую сумму.", False)

        created = 0
        fallback_category = _fallback_category_id(session, user_id)
        for item in receipt.items:
            amount = item.amount
            if amount is None:
                continue
            category_id = item.category_id or fallback_category
            if category_id is None:
                continue
            transaction = Transaction(
                user_id=user_id,
                category_id=category_id,
                amount=amount,
                currency=receipt.currency or "RUB",
                is_approximate=False,
                comment=f"Receipt {receipt.id} — {item.item_name or 'позиция'}",
                source="email" if receipt.source == "email" else receipt.source,
                receipt_id=receipt.id,
            )
            session.add(transaction)
            created += 1
        if created == 0:
            return ("Не удалось создать транзакции: нет категорий у позиций.", False)

        receipt.status = "confirmed"
        session.flush()

    _cleanup_receipt_file(receipt)
    return (f"Создано транзакций: {created}.", True)


def decline_receipt(user_id: int, receipt_id: int, *, delete_file: bool = True) -> tuple[str, bool]:
    """Mark receipt as failed and optionally remove file."""
    with session_scope() as session:
        receipt = _get_receipt_for_user(session, user_id, receipt_id)
        if receipt is None:
            return ("Чек не найден или нет доступа.", False)
        if receipt.status != "pending":
            return ("Чек уже обработан.", False)
        receipt.status = "failed"
        session.flush()

    if delete_file:
        _cleanup_receipt_file(receipt)
    return ("Чек отклонён.", True)


def _cleanup_receipt_file(receipt: Receipt) -> None:
    file_path = receipt.original_file
    if not file_path:
        return
    try:
        os.remove(file_path)
    except FileNotFoundError:
        return
    except OSError as exc:  # noqa: BLE001
        LOGGER.warning("receipt_file_cleanup_failed", receipt_id=receipt.id, error=str(exc))


def _get_receipt_for_user(session: Session, user_id: int, receipt_id: int) -> Receipt | None:
    return (
        session.execute(select(Receipt).where(Receipt.id == receipt_id, Receipt.user_id == user_id))
        .scalars()
        .one_or_none()
    )


def _resolve_total_category(session: Session, receipt: Receipt) -> int | None:
    category_id = None
    if receipt.items:
        non_null = [item.category_id for item in receipt.items if item.category_id is not None]
        if len(set(non_null)) == 1:
            category_id = non_null[0]
    if category_id is None:
        category_id = _fallback_category_id(session, receipt.user_id)
    return category_id


def _fallback_category_id(session: Session, user_id: int) -> int | None:
    stmt: Select[tuple[int]] = (
        select(Category.id).where(Category.id.isnot(None)).order_by(Category.id.asc()).limit(1)
    )
    result = session.execute(stmt).first()
    if result is None:
        return None
    return result[0]


def _as_decimal(value: float | Decimal | None) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


__all__ = [
    "InboxEntry",
    "ReceiptLine",
    "confirm_receipt_items",
    "confirm_receipt_total",
    "decline_receipt",
    "ensure_receipts_dir",
    "format_inbox_entry",
    "list_inbox_entries",
    "store_receipt_file",
]
