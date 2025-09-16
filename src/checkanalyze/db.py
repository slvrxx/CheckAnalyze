"""Database helpers and ORM models."""
from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Generator, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    create_engine,
)
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

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    name: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    transactions: Mapped[list["Transaction"]] = relationship("Transaction", back_populates="user")


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    group_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    category_id: Mapped[int] = mapped_column(Integer, ForeignKey("categories.id"), nullable=False)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    amount: Mapped[float] = mapped_column(Numeric(scale=2))
    currency: Mapped[str] = mapped_column(String(3), default="RUB")
    is_approximate: Mapped[bool] = mapped_column(Boolean, default=False)
    comment: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship("User", back_populates="transactions")


class Receipt(Base):
    """Stored receipt metadata for interactive review."""

    __tablename__ = "receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    merchant_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("merchant_profiles.id"))
    merchant_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    merchant_inn: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="RUB")
    total_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="telegram")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    merchant: Mapped[Optional["MerchantProfile"]] = relationship("MerchantProfile")
    items: Mapped[list["ReceiptItem"]] = relationship("ReceiptItem", back_populates="receipt")


class MerchantProfile(Base):
    """Merchant metadata for better predictions."""

    __tablename__ = "merchant_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    inn: Mapped[Optional[str]] = mapped_column(String(20), index=True)
    merchant_hash: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    default_category_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("categories.id"))
    hints: Mapped[dict] = mapped_column(JSON, default=dict)

    category: Mapped[Optional[Category]] = relationship("Category")
    receipt_items: Mapped[list["ReceiptItem"]] = relationship("ReceiptItem", back_populates="merchant")


class ReceiptItem(Base):
    """Extracted receipt line items."""

    __tablename__ = "receipt_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    merchant_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("merchant_profiles.id"))
    receipt_id: Mapped[int] = mapped_column(Integer, ForeignKey("receipts.id"), nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)
    quantity: Mapped[float] = mapped_column(Float, default=1.0)
    amount: Mapped[float] = mapped_column(Numeric(scale=2))
    currency: Mapped[str] = mapped_column(String(3), default="RUB")
    is_income: Mapped[bool] = mapped_column(Boolean, default=False)
    predicted_category_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("categories.id"))
    selected_category_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("categories.id"))
    is_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    line_number: Mapped[int] = mapped_column(Integer, default=1)

    merchant: Mapped[Optional[MerchantProfile]] = relationship("MerchantProfile", back_populates="receipt_items")
    predicted_category: Mapped[Optional[Category]] = relationship("Category", foreign_keys=[predicted_category_id])
    selected_category: Mapped[Optional[Category]] = relationship("Category", foreign_keys=[selected_category_id])
    receipt: Mapped["Receipt"] = relationship("Receipt", back_populates="items")


class CategoryTrainingSample(Base):
    """Stores labelled samples for incremental learning."""

    __tablename__ = "category_training_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    merchant_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("merchant_profiles.id"))
    description: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="RUB")
    category_id: Mapped[int] = mapped_column(Integer, ForeignKey("categories.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    merchant: Mapped[Optional[MerchantProfile]] = relationship("MerchantProfile")
    category: Mapped[Category] = relationship("Category")


class EmailIdentity(Base):
    """Maps email addresses to users for mailbox ingestion."""

    __tablename__ = "email_identities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)


_engine = create_engine(CONFIG.database.url, future=True)
SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)


def init_schema() -> None:
    """Create tables if they do not exist."""
    Base.metadata.create_all(_engine)


@contextlib.contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Provide a transactional scope around a series of operations."""
    session = SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
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
    "MerchantProfile",
    "ReceiptItem",
    "CategoryTrainingSample",
    "EmailIdentity",
    "SessionFactory",
    "session_scope",
    "init_schema",
]
