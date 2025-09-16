"""Receipt document parsing utilities."""
from __future__ import annotations

from dataclasses import dataclass, field
import io
import re
from typing import Iterable, List, Optional

import pdfplumber
from pdf2image import convert_from_bytes
import pytesseract
import structlog

logger = structlog.get_logger(__name__)

CURRENCY_RE = re.compile(r"(RUB|RUR|USD|EUR|KZT|BYN|\u20bd)", re.IGNORECASE)
AMOUNT_RE = re.compile(r"(?P<value>\d+[\s\u00A0]?[\d\s\u00A0]*[\.,]\d{2}|\d+)")
INN_RE = re.compile(r"ИНН\s*[:№]*\s*(?P<inn>[0-9]{10,12})")


@dataclass(slots=True)
class ReceiptItemCandidate:
    description: str
    amount: float
    quantity: float = 1.0
    currency: str = "RUB"


@dataclass(slots=True)
class ReceiptParseResult:
    merchant_name: Optional[str] = None
    merchant_inn: Optional[str] = None
    currency: str = "RUB"
    total_amount: Optional[float] = None
    items: List[ReceiptItemCandidate] = field(default_factory=list)


def _normalize_amount(text: str) -> Optional[float]:
    try:
        cleaned = text.replace(" ", "").replace("\u00A0", "").replace(",", ".")
        return float(cleaned)
    except (ValueError, AttributeError):
        return None


def _extract_pdf_text(data: bytes) -> str:
    """Extract text from a PDF, falling back to OCR if needed."""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    if text.strip():
        return text
    images = convert_from_bytes(data)
    return "\n".join(pytesseract.image_to_string(img, lang="rus+eng") for img in images)


def _split_lines(text: str) -> Iterable[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def parse_receipt(data: bytes) -> ReceiptParseResult:
    """Parse a receipt PDF and return structured data."""
    text = _extract_pdf_text(data)
    lines = list(_split_lines(text))
    logger.debug("parsed_lines", count=len(lines))

    result = ReceiptParseResult()

    # Merchant name heuristics: first non-numeric line longer than 5 characters
    for line in lines[:5]:
        if any(char.isalpha() for char in line) and not AMOUNT_RE.search(line):
            result.merchant_name = line.strip()
            break

    # Extract INN
    for line in lines:
        match = INN_RE.search(line)
        if match:
            result.merchant_inn = match.group("inn")
            break

    # Detect currency and total
    for line in lines[::-1]:
        if "итог" in line.lower() or "итого" in line.lower() or "total" in line.lower():
            amount_match = AMOUNT_RE.search(line)
            if amount_match:
                result.total_amount = _normalize_amount(amount_match.group("value"))
            currency_match = CURRENCY_RE.search(line)
            if currency_match:
                result.currency = _normalize_currency(currency_match.group(1))
            break

    # Parse items heuristically: lines containing an amount and some text.
    for line in lines:
        amount_match = AMOUNT_RE.search(line)
        if not amount_match:
            continue
        amount_value = _normalize_amount(amount_match.group("value"))
        if amount_value is None:
            continue
        head = line[: amount_match.start()].strip(" :\t\u00A0")
        if not head or len(head) < 3:
            continue
        currency_match = CURRENCY_RE.search(line[amount_match.end() :])
        currency = result.currency
        if currency_match:
            currency = _normalize_currency(currency_match.group(1))
        item = ReceiptItemCandidate(description=head, amount=amount_value, currency=currency)
        result.items.append(item)

    if not result.items and result.total_amount is not None:
        result.items.append(ReceiptItemCandidate(description="TOTAL", amount=result.total_amount, currency=result.currency))

    return result


def _normalize_currency(token: str) -> str:
    token = token.upper().replace("RUR", "RUB").replace("\u20BD", "RUB")
    allowed = {"RUB", "USD", "EUR", "KZT", "BYN"}
    if token in allowed:
        return token
    return "RUB"


__all__ = ["ReceiptItemCandidate", "ReceiptParseResult", "parse_receipt"]
