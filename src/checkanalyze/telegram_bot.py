"""Telegram bot for CheckAnalyze."""
from __future__ import annotations

import asyncio
import io
from typing import Optional

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import structlog

from .config import CONFIG
from .db import User, init_schema, session_scope
from .service import (
    confirm_receipt,
    format_categories_message,
    get_receipt_summary,
    list_pending_receipts,
    process_receipt_bytes,
    set_receipt_item_category,
)

logger = structlog.get_logger(__name__)


def _ensure_user(telegram_id: int, name: Optional[str]) -> User:
    with session_scope() as session:
        user = session.query(User).filter(User.telegram_id == telegram_id).one_or_none()
        if user:
            return user
        user = User(telegram_id=telegram_id, name=name)
        session.add(user)
        session.flush()
        logger.info("user_created", telegram_id=telegram_id)
        return user


def _is_allowed(telegram_id: int) -> bool:
    allowed_chat = CONFIG.telegram.allowed_chat_id
    if allowed_chat and telegram_id != allowed_chat:
        logger.warning("unauthorized_user", telegram_id=telegram_id)
        return False
    return True


async def _handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    telegram_user = update.effective_user

    if not _is_allowed(telegram_user.id):
        return

    document = update.message.document if update.message else None
    if not document:
        return

    file = await document.get_file(read_timeout=30)
    bio = io.BytesIO()
    await file.download_to_memory(out=bio)
    data = bio.getvalue()

    user = _ensure_user(telegram_user.id, telegram_user.full_name)

    processed = process_receipt_bytes(user.id, data, source="telegram")

    context.user_data["last_receipt_id"] = processed.receipt_id
    await update.message.reply_text(processed.summary_text)


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    telegram_user = update.effective_user
    if not _is_allowed(telegram_user.id):
        return
    user = _ensure_user(telegram_user.id, telegram_user.full_name)
    await update.message.reply_text(
        "Привет! Отправь мне чек в PDF — я проанализирую его, предложу категории и дам команды для учёта."
    )


async def _categories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    telegram_user = update.effective_user
    if not _is_allowed(telegram_user.id):
        return
    user = _ensure_user(telegram_user.id, telegram_user.full_name)
    message = format_categories_message(user.id)
    await update.message.reply_text(message)


async def _cat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    telegram_user = update.effective_user
    if not _is_allowed(telegram_user.id):
        return
    user = _ensure_user(telegram_user.id, telegram_user.full_name)

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Использование: /cat <номер строки> <категория> или /cat <чек> <номер> <категория>."
        )
        return

    try:
        if len(args) == 2:
            receipt_id = context.user_data.get("last_receipt_id")
            if receipt_id is None:
                await update.message.reply_text(
                    "Не найден активный чек. Укажите ID: /cat <чек> <номер> <категория>."
                )
                return
            line_number = int(args[0])
            category_token = args[1]
        else:
            receipt_id = int(args[0])
            line_number = int(args[1])
            category_token = " ".join(args[2:])
    except ValueError:
        await update.message.reply_text("Неверный формат команды /cat.")
        return

    message, success = set_receipt_item_category(user.id, receipt_id, line_number, category_token)
    await update.message.reply_text(message)
    if success:
        context.user_data["last_receipt_id"] = receipt_id
        summary = get_receipt_summary(user.id, receipt_id)
        if summary:
            await update.message.reply_text(summary)


async def _confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    telegram_user = update.effective_user
    if not _is_allowed(telegram_user.id):
        return
    user = _ensure_user(telegram_user.id, telegram_user.full_name)

    receipt_id = None
    if context.args:
        try:
            receipt_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("ID чека должен быть числом.")
            return
    else:
        receipt_id = context.user_data.get("last_receipt_id")

    if receipt_id is None:
        await update.message.reply_text("Нет активного чека. Укажите ID: /confirm <id>.")
        return

    message, success = confirm_receipt(user.id, receipt_id)
    await update.message.reply_text(message)
    if success and context.user_data.get("last_receipt_id") == receipt_id:
        context.user_data.pop("last_receipt_id", None)


async def _pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    telegram_user = update.effective_user
    if not _is_allowed(telegram_user.id):
        return
    user = _ensure_user(telegram_user.id, telegram_user.full_name)
    message = list_pending_receipts(user.id)
    await update.message.reply_text(message)


async def _receipt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not context.args:
        return
    telegram_user = update.effective_user
    if not _is_allowed(telegram_user.id):
        return
    user = _ensure_user(telegram_user.id, telegram_user.full_name)
    try:
        receipt_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID чека должен быть числом.")
        return
    summary = get_receipt_summary(user.id, receipt_id)
    if not summary:
        await update.message.reply_text("Чек не найден.")
        return
    context.user_data["last_receipt_id"] = receipt_id
    await update.message.reply_text(summary)


def build_application() -> Application:
    init_schema()
    application = ApplicationBuilder().token(CONFIG.telegram.token).build()
    application.add_handler(CommandHandler("start", _start))
    application.add_handler(CommandHandler("categories", _categories))
    application.add_handler(CommandHandler("cat", _cat))
    application.add_handler(CommandHandler("confirm", _confirm))
    application.add_handler(CommandHandler("pending", _pending))
    application.add_handler(CommandHandler("receipt", _receipt))
    application.add_handler(MessageHandler(filters.Document.PDF, _handle_document))
    return application


async def run_bot() -> None:
    application = build_application()
    await application.initialize()
    await application.start()
    logger.info("telegram_bot_started")
    await application.updater.start_polling()
    try:
        await asyncio.Event().wait()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


def run() -> None:
    asyncio.run(run_bot())


__all__ = ["run", "run_bot", "build_application"]
