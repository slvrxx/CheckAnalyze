"""Database helpers and ORM models."""

from __future__ import annotations

import contextlib
from collections.abc import Generator
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    create_engine,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from .config import CONFIG


class Base(DeclarativeBase):
    """Base declarative class."""


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, unique=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    transactions: Mapped[list[Transaction]] = relationship("Transaction", back_populates="user")
    receipts: Mapped[list[Receipt]] = relationship("Receipt", back_populates="user")


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    transactions: Mapped[list[Transaction]] = relationship("Transaction", back_populates="category")
    receipt_items: Mapped[list[ReceiptItem]] = relationship(
        "ReceiptItem", back_populates="category"
    )


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    category_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("categories.id"), nullable=False
    )
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="RUB")
    is_approximate: Mapped[bool] = mapped_column(Boolean, default=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    receipt_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("receipts.id"), nullable=True
    )
    source: Mapped[str | None] = mapped_column(String(16), nullable=True)

    user: Mapped[User] = relationship("User", back_populates="transactions")
    category: Mapped[Category] = relationship("Category", back_populates="transactions")
    receipt: Mapped[Receipt | None] = relationship("Receipt", back_populates="transactions")


class Receipt(Base):
    __tablename__ = "receipts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    source: Mapped[str] = mapped_column(
        Enum("telegram", "email", "manual", name="receipt_source", create_type=False)
    )
    original_file: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    json_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    total_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    tip_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True, default=0)
    merchant_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    merchant_inn: Mapped[str | None] = mapped_column(String(32), nullable=True)
    purchased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship("User", back_populates="receipts")
    items: Mapped[list[ReceiptItem]] = relationship("ReceiptItem", back_populates="receipt")
    transactions: Mapped[list[Transaction]] = relationship("Transaction", back_populates="receipt")


class ReceiptItem(Base):
    __tablename__ = "receipt_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    receipt_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("receipts.id"), nullable=False)
    line_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    item_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    qty: Mapped[float | None] = mapped_column(Numeric(12, 3), nullable=True)
    unit_price: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    category_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("categories.id"),
        nullable=True,
    )

    receipt: Mapped[Receipt] = relationship("Receipt", back_populates="items")
    category: Mapped[Category | None] = relationship("Category", back_populates="receipt_items")


class EmailInbox(Base):
    __tablename__ = "email_inbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=True)
    message_id: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)
    from_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User | None] = relationship("User")


_engine = create_engine(CONFIG.database.url, future=True)
SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)


def init_schema() -> None:
    Base.metadata.create_all(_engine)


@contextlib.contextmanager
def session_scope() -> Generator[Session, None, None]:
    session = SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:  # noqa: BLE001
        session.rollback()
        raise
    finally:
        session.close()


__all__ = [
    "Base",
    "User",
    "Category",
    "Transaction",
    "Receipt",
    "ReceiptItem",
    "EmailInbox",
    "SessionFactory",
    "session_scope",
    "init_schema",
]
