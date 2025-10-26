"""Telegram bot integration for managing receipt inbox."""

from __future__ import annotations

import asyncio
import io

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import CONFIG
from .db import User, session_scope
from .service import (
    confirm_receipt_items,
    confirm_receipt_total,
    decline_receipt,
    ensure_receipts_dir,
    format_inbox_entry,
    list_inbox_entries,
    store_receipt_file,
)

LOGGER = structlog.get_logger(__name__)


def _is_authorized(telegram_id: int) -> bool:
    admin_id = CONFIG.telegram.admin_id
    if admin_id is not None and telegram_id != admin_id:
        LOGGER.warning("unauthorized_user", telegram_id=telegram_id)
        return False
    return True


def _get_or_create_user(telegram_id: int, name: str | None) -> User:
    with session_scope() as session:
        user = session.query(User).filter(User.telegram_id == telegram_id).one_or_none()
        if user:
            return user
        user = User(telegram_id=telegram_id, name=name)
        session.add(user)
        session.flush()
        session.refresh(user)
        LOGGER.info("telegram_user_created", user_id=user.id, telegram_id=telegram_id)
        return user


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    telegram_user = update.effective_user
    if not _is_authorized(telegram_user.id):
        return
    _get_or_create_user(telegram_user.id, telegram_user.full_name)
    await update.message.reply_text(
        "Отправьте PDF или изображение с чеком. "
        "Используйте команду /inbox для работы с входящими чеками."
    )


async def _handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    telegram_user = update.effective_user
    if not _is_authorized(telegram_user.id):
        return

    document = update.message.document
    if document is None:
        return
    file = await document.get_file(read_timeout=30)
    buffer = io.BytesIO()
    await file.download_to_memory(out=buffer)
    buffer.seek(0)

    user = _get_or_create_user(telegram_user.id, telegram_user.full_name)
    file_name = document.file_name or "receipt.pdf"
    data = buffer.read()
    receipt = store_receipt_file(user.id, data, file_name, source="telegram")
    await update.message.reply_text(
        f"Чек сохранён с номером {receipt.id}. Проверьте /inbox для подтверждения."
    )


async def _inbox(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    telegram_user = update.effective_user
    if not _is_authorized(telegram_user.id):
        return

    user = _get_or_create_user(telegram_user.id, telegram_user.full_name)
    entries = list_inbox_entries(user.id)
    if not entries:
        await update.message.reply_text("Новых чеков нет.")
        return

    for entry in entries:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Подтвердить общую сумму",
                        callback_data=f"receipt:total:{entry.receipt.id}",
                    ),
                    InlineKeyboardButton(
                        "Разнести по позициям",
                        callback_data=f"receipt:items:{entry.receipt.id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "Отклонить",
                        callback_data=f"receipt:decline:{entry.receipt.id}",
                    )
                ],
            ]
        )
        await update.message.reply_text(format_inbox_entry(entry), reply_markup=keyboard)


async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or update.effective_user is None:
        return
    await query.answer()
    telegram_user = update.effective_user
    if not _is_authorized(telegram_user.id):
        return

    data = query.data or ""
    try:
        _, action, receipt_id_str = data.split(":", 2)
        receipt_id = int(receipt_id_str)
    except ValueError:
        await query.edit_message_text("Некорректный запрос.")
        return

    user = _get_or_create_user(telegram_user.id, telegram_user.full_name)

    if action == "total":
        message, success = confirm_receipt_total(user.id, receipt_id)
    elif action == "items":
        message, success = confirm_receipt_items(user.id, receipt_id)
    elif action == "decline":
        message, success = decline_receipt(user.id, receipt_id)
    else:
        message, success = ("Неизвестное действие.", False)

    if success:
        await query.edit_message_text(f"{query.message.text}\n\n✅ {message}")
    else:
        await query.edit_message_text(f"{query.message.text}\n\n⚠️ {message}")


def build_application() -> Application:
    ensure_receipts_dir()
    application = ApplicationBuilder().token(CONFIG.telegram.token).build()
    application.add_handler(CommandHandler("start", _start))
    application.add_handler(CommandHandler("inbox", _inbox))
    application.add_handler(MessageHandler(filters.Document.ALL, _handle_document))
    application.add_handler(CallbackQueryHandler(_handle_callback, pattern=r"^receipt:"))
    return application


async def run_bot() -> None:
    application = build_application()
    await application.initialize()
    await application.start()
    LOGGER.info("telegram_bot_started")
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
