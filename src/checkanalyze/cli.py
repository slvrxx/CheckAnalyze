"""Command line interface for CheckAnalyze."""

from __future__ import annotations

import asyncio

import structlog
import typer

from .service import (
    confirm_receipt_items,
    confirm_receipt_total,
    decline_receipt,
    format_inbox_entry,
    list_inbox_entries,
)
from .telegram_bot import run_bot

app = typer.Typer(help="CheckAnalyze service commands")
receipts_app = typer.Typer(help="Утилиты для работы с чеками")
app.add_typer(receipts_app, name="receipts")
LOGGER = structlog.get_logger(__name__)


@app.command()
def telegram() -> None:
    """Run the Telegram bot."""
    LOGGER.info("telegram_bot_starting")
    asyncio.run(run_bot())


@receipts_app.command("inbox")
def receipts_inbox(user_id: int = typer.Option(..., help="ID пользователя")) -> None:
    entries = list_inbox_entries(user_id)
    if not entries:
        typer.echo("Нет входящих чеков.")
        raise typer.Exit()
    for entry in entries:
        typer.echo(format_inbox_entry(entry))
        typer.echo("-")


@receipts_app.command("confirm-total")
def receipts_confirm_total(
    receipt_id: int = typer.Argument(..., help="ID чека"),
    user_id: int = typer.Option(..., help="ID пользователя"),
) -> None:
    message, success = confirm_receipt_total(user_id, receipt_id)
    typer.echo(message)
    if not success:
        raise typer.Exit(code=1)


@receipts_app.command("confirm-items")
def receipts_confirm_items(
    receipt_id: int = typer.Argument(..., help="ID чека"),
    user_id: int = typer.Option(..., help="ID пользователя"),
) -> None:
    message, success = confirm_receipt_items(user_id, receipt_id)
    typer.echo(message)
    if not success:
        raise typer.Exit(code=1)


@receipts_app.command("decline")
def receipts_decline(
    receipt_id: int = typer.Argument(..., help="ID чека"),
    user_id: int = typer.Option(..., help="ID пользователя"),
) -> None:
    message, success = decline_receipt(user_id, receipt_id)
    typer.echo(message)
    if not success:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
