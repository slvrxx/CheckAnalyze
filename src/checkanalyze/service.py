"""Shared business logic for processing receipts."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
from typing import Iterable, List, Optional, Sequence

import structlog

from .db import (
    Category,
    CategoryTrainingSample,
    MerchantProfile,
    Receipt,
    ReceiptItem,
    session_scope,
)
from .processing.categorizer import Categorizer
from .processing.extraction import ReceiptParseResult, parse_receipt

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class ProcessedReceiptItem:
    item_id: int
    line_number: int
    description: str
    amount: float
    currency: str
    selected_category_name: Optional[str]
    confidence: float


@dataclass(slots=True)
class ProcessedReceipt:
    receipt_id: int
    parse_result: ReceiptParseResult
    items: List[ProcessedReceiptItem]
    summary_text: str
    merchant: Optional[MerchantProfile]


def process_receipt_bytes(user_id: int, data: bytes, source: str = "telegram") -> ProcessedReceipt:
    """Parse, categorise and persist a receipt for a given user."""
    parse_result = parse_receipt(data)
    merchant = _find_or_create_merchant(parse_result.merchant_name, parse_result.merchant_inn)
    categories = list(_fetch_categories(user_id))
    categorizer = Categorizer()
    predictions = categorizer.predict(parse_result, categories)

    stored_items: list[ProcessedReceiptItem] = []
    with session_scope() as session:
        receipt = Receipt(
            user_id=user_id,
            merchant_id=merchant.id if merchant else None,
            merchant_name=parse_result.merchant_name,
            merchant_inn=parse_result.merchant_inn,
            currency=parse_result.currency,
            total_amount=parse_result.total_amount,
            source=source,
        )
        session.add(receipt)
        session.flush()

        for index, (candidate, prediction) in enumerate(zip(parse_result.items, predictions), start=1):
            selected_category_id = prediction.category_id
            receipt_item = ReceiptItem(
                user_id=user_id,
                merchant_id=merchant.id if merchant else None,
                receipt_id=receipt.id,
                description=candidate.description,
                quantity=candidate.quantity,
                amount=candidate.amount,
                currency=candidate.currency,
                is_income=False,
                predicted_category_id=prediction.category_id,
                selected_category_id=selected_category_id,
                confidence=prediction.confidence,
                line_number=index,
            )
            session.add(receipt_item)
            session.flush()

            stored_items.append(
                ProcessedReceiptItem(
                    item_id=receipt_item.id,
                    line_number=index,
                    description=candidate.description,
                    amount=candidate.amount,
                    currency=candidate.currency,
                    selected_category_name=_category_name(categories, selected_category_id),
                    confidence=prediction.confidence,
                )
            )

        summary_text = build_receipt_summary(receipt, stored_items, include_instructions=True)

    return ProcessedReceipt(
        receipt_id=receipt.id,
        parse_result=parse_result,
        items=stored_items,
        summary_text=summary_text,
        merchant=merchant,
    )


def _category_name(categories: Sequence[Category], category_id: Optional[int]) -> Optional[str]:
    if category_id is None:
        return None
    for category in categories:
        if category.id == category_id:
            return category.name
    return None


def _fetch_categories(user_id: int) -> Iterable[Category]:
    with session_scope() as session:
        categories = (
            session.query(Category)
            .filter((Category.user_id == user_id) | (Category.user_id.is_(None)))
            .order_by(Category.name)
            .all()
        )
        for category in categories:
            yield category


def _find_or_create_merchant(name: Optional[str], inn: Optional[str]) -> Optional[MerchantProfile]:
    if not name and not inn:
        return None

    with session_scope() as session:
        merchant = None
        if inn:
            merchant = session.query(MerchantProfile).filter(MerchantProfile.inn == inn).one_or_none()
        if not merchant and name:
            merchant = (
                session.query(MerchantProfile)
                .filter(MerchantProfile.name.ilike(f"%{name}%"))
                .first()
            )
        if merchant:
            return merchant

        merchant = MerchantProfile(
            name=name or inn or "UNKNOWN",
            inn=inn,
            merchant_hash=_merchant_hash(name or inn or "UNKNOWN"),
        )
        session.add(merchant)
        session.commit()
        logger.info("merchant_created", merchant_id=merchant.id)
        return merchant


def _merchant_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_receipt_summary(
    receipt: Receipt,
    items: Sequence[ProcessedReceiptItem] | Sequence[ReceiptItem],
    *,
    include_instructions: bool = False,
) -> str:
    """Build a human-readable summary of receipt items."""
    header_parts = [f"Чек #{receipt.id}"]
    if receipt.merchant_name:
        header_parts.append(receipt.merchant_name)
    if receipt.merchant_inn:
        header_parts.append(f"ИНН {receipt.merchant_inn}")
    header = " — ".join(header_parts)

    lines = [header]
    for item in items:
        if isinstance(item, ReceiptItem):
            category_name = (
                item.selected_category.name
                if item.selected_category is not None
                else None
            )
            amount = float(item.amount)
            currency = item.currency
            description = item.description
            confidence = item.confidence or 0.0
            line_number = item.line_number
        else:
            category_name = item.selected_category_name
            amount = item.amount
            currency = item.currency
            description = item.description
            confidence = item.confidence
            line_number = item.line_number

        category_label = category_name or "UNKNOWN"
        lines.append(
            f"{line_number}. {description} — {amount:.2f} {currency} → {category_label} (p={confidence:.2f})"
        )

    if receipt.total_amount:
        lines.append("")
        lines.append(f"Итого: {receipt.total_amount:.2f} {receipt.currency}")

    if include_instructions:
        lines.append("")
        lines.append("Изменить категорию: /cat <номер> <код или название>")
        lines.append("Список категорий: /categories")
        lines.append("Подтвердить чек: /confirm")

    return "\n".join(lines)


def format_categories_message(user_id: int) -> str:
    categories = list(_fetch_categories(user_id))
    if not categories:
        return "Категории не найдены. Добавьте категории в основной системе учёта."
    lines = ["Доступные категории:"]
    for category in categories:
        code = category.code or "—"
        lines.append(f"{category.id}: [{code}] {category.name}")
    return "\n".join(lines)


def set_receipt_item_category(
    user_id: int,
    receipt_id: int,
    line_number: int,
    category_token: str,
) -> tuple[str, bool]:
    """Assign a category to a receipt item by its position."""
    category_token = category_token.strip()
    with session_scope() as session:
        receipt = session.query(Receipt).filter(Receipt.id == receipt_id, Receipt.user_id == user_id).one_or_none()
        if not receipt:
            return "Чек не найден или принадлежит другому пользователю.", False

        item = (
            session.query(ReceiptItem)
            .filter(ReceiptItem.receipt_id == receipt.id, ReceiptItem.line_number == line_number)
            .one_or_none()
        )
        if not item:
            return "Строка с таким номером не найдена.", False

        category = _resolve_category(session, user_id, category_token)
        if not category:
            return "Категория не найдена. Используйте /categories для просмотра списка.", False

        item.selected_category_id = category.id
        item.is_manual = True
        session.flush()

        message = (
            f"Строка {line_number} теперь в категории '{category.name}'."
        )
        return message, True


def _resolve_category(session, user_id: int, token: str) -> Optional[Category]:
    token = token.strip()
    # Try direct ID
    if token.isdigit():
        category = (
            session.query(Category)
            .filter(Category.id == int(token), (Category.user_id == user_id) | (Category.user_id.is_(None)))
            .one_or_none()
        )
        if category:
            return category

    # Try code match
    category = (
        session.query(Category)
        .filter(
            (Category.user_id == user_id) | (Category.user_id.is_(None)),
            Category.code.isnot(None),
            Category.code.ilike(token),
        )
        .first()
    )
    if category:
        return category

    # Try name match (case-insensitive contains)
    category = (
        session.query(Category)
        .filter((Category.user_id == user_id) | (Category.user_id.is_(None)))
        .filter(Category.name.ilike(f"%{token}%"))
        .first()
    )
    return category


def confirm_receipt(user_id: int, receipt_id: int) -> tuple[str, bool]:
    """Mark receipt as confirmed and record training samples."""
    with session_scope() as session:
        receipt = session.query(Receipt).filter(Receipt.id == receipt_id, Receipt.user_id == user_id).one_or_none()
        if not receipt:
            return "Чек не найден или принадлежит другому пользователю.", False

        items = session.query(ReceiptItem).filter(ReceiptItem.receipt_id == receipt.id).order_by(ReceiptItem.line_number).all()
        if not items:
            return "В чеке нет позиций для подтверждения.", False

        for item in items:
            if item.selected_category_id is None:
                return f"У строки {item.line_number} не выбрана категория.", False

        for item in items:
            sample = CategoryTrainingSample(
                user_id=user_id,
                merchant_id=receipt.merchant_id,
                description=item.description,
                amount=float(item.amount),
                currency=item.currency,
                category_id=item.selected_category_id,
            )
            session.add(sample)

        receipt.status = "confirmed"
        session.flush()

        if receipt.merchant_id and items:
            _maybe_update_merchant_default(session, receipt.merchant_id, items)

        summary = build_receipt_summary(receipt, items, include_instructions=False)
        commands = _format_final_commands(items)

    # Retrain categorizer with the new samples
    Categorizer()

    return f"{summary}\n\n{commands}", True


def _format_final_commands(items: Sequence[ReceiptItem]) -> str:
    lines = ["Команды для записи:"]

    for item in items:
        entry_type = "I" if item.is_income else "E"
        amount_value = _format_amount(float(item.amount))
        currency = (item.currency or "RUB").upper()
        category_token = _category_token(item)
        accuracy_flag = 1  # PDF суммы считаем точными
        comment = _format_comment(item.description)

        lines.append(
            f"{entry_type} {amount_value} {currency} {category_token} {accuracy_flag} {comment}"
        )

    return "\n".join(lines)


def _format_amount(value: float) -> str:
    text = f"{value:.2f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _category_token(item: ReceiptItem) -> str:
    category = item.selected_category
    if category and category.code:
        return category.code
    if category and category.id:
        return str(category.id)
    if item.selected_category_id:
        return str(item.selected_category_id)
    if item.predicted_category_id:
        return str(item.predicted_category_id)
    return "UNKNOWN"


def _format_comment(description: str) -> str:
    return " ".join(description.strip().split()) or "—"


def _maybe_update_merchant_default(session, merchant_id: int, items: Sequence[ReceiptItem]) -> None:
    merchant = session.query(MerchantProfile).filter(MerchantProfile.id == merchant_id).one_or_none()
    if not merchant:
        return
    category_ids = [item.selected_category_id for item in items if item.selected_category_id]
    if not category_ids:
        return
    most_common_id, count = Counter(category_ids).most_common(1)[0]
    if count >= 2:
        merchant.default_category_id = most_common_id


def get_receipt_summary(user_id: int, receipt_id: int) -> Optional[str]:
    with session_scope() as session:
        receipt = session.query(Receipt).filter(Receipt.id == receipt_id, Receipt.user_id == user_id).one_or_none()
        if not receipt:
            return None
        items = (
            session.query(ReceiptItem)
            .filter(ReceiptItem.receipt_id == receipt.id)
            .order_by(ReceiptItem.line_number)
            .all()
        )
        return build_receipt_summary(receipt, items, include_instructions=True)


def list_pending_receipts(user_id: int) -> str:
    with session_scope() as session:
        receipts = (
            session.query(Receipt)
            .filter(Receipt.user_id == user_id, Receipt.status == "pending")
            .order_by(Receipt.created_at.desc())
            .all()
        )
        if not receipts:
            return "Нет неподтверждённых чеков."
        lines = ["Неподтверждённые чеки:"]
        for receipt in receipts:
            merchant = receipt.merchant_name or "Без названия"
            lines.append(
                f"#{receipt.id} — {merchant} ({receipt.created_at.strftime('%Y-%m-%d %H:%M')})"
            )
        lines.append("\nИспользуйте /receipt <id>, чтобы посмотреть чек.")
        return "\n".join(lines)


__all__ = [
    "ProcessedReceipt",
    "ProcessedReceiptItem",
    "process_receipt_bytes",
    "format_categories_message",
    "set_receipt_item_category",
    "confirm_receipt",
    "get_receipt_summary",
    "list_pending_receipts",
]
