"""Command line interface for CheckAnalyze."""
from __future__ import annotations

import asyncio
import typer
import structlog

from .db import EmailIdentity, init_schema, session_scope
from .email_ingest import EmailIngestor
from .service import (
    confirm_receipt,
    format_categories_message,
    get_receipt_summary,
    list_pending_receipts,
)
from .telegram_bot import run_bot

app = typer.Typer(help="CheckAnalyze service commands")
receipts_app = typer.Typer(help="Утилиты для работы с чеками")
app.add_typer(receipts_app, name="receipts")
logger = structlog.get_logger(__name__)


@app.command()
def initdb() -> None:
    """Initialise database tables."""
    init_schema()
    typer.echo("Database schema ensured.")


@app.command()
def telegram() -> None:
    """Run the Telegram bot."""
    init_schema()
    asyncio.run(run_bot())


@app.command()
def email_worker(forever: bool = typer.Option(True, help="Poll mailbox continuously.")) -> None:
    """Run the email ingestion worker."""
    worker = EmailIngestor()
    if forever:
        worker.run_forever()
    else:
        processed = worker.poll_once()
        typer.echo(f"Processed {processed} messages")


@receipts_app.command("pending")
def receipts_pending(user_id: int = typer.Option(..., help="ID пользователя")) -> None:
    typer.echo(list_pending_receipts(user_id))


@receipts_app.command("show")
def receipts_show(
    receipt_id: int = typer.Argument(..., help="ID чека"),
    user_id: int = typer.Option(..., help="ID пользователя"),
) -> None:
    summary = get_receipt_summary(user_id, receipt_id)
    if not summary:
        typer.echo("Чек не найден или нет доступа.")
        raise typer.Exit(code=1)
    typer.echo(summary)


@receipts_app.command("confirm")
def receipts_confirm(
    receipt_id: int = typer.Argument(..., help="ID чека"),
    user_id: int = typer.Option(..., help="ID пользователя"),
) -> None:
    message, success = confirm_receipt(user_id, receipt_id)
    typer.echo(message)
    if not success:
        raise typer.Exit(code=1)


@receipts_app.command("categories")
def receipts_categories(user_id: int = typer.Option(..., help="ID пользователя")) -> None:
    typer.echo(format_categories_message(user_id))


@receipts_app.command("link-email")
def receipts_link_email(
    email: str = typer.Argument(..., help="Email адрес"),
    user_id: int = typer.Option(..., help="ID пользователя"),
) -> None:
    normalized = email.strip().lower()
    with session_scope() as session:
        existing = (
            session.query(EmailIdentity).filter(EmailIdentity.email == normalized).one_or_none()
        )
        if existing:
            existing.user_id = user_id
            action = "обновлён"
        else:
            identity = EmailIdentity(email=normalized, user_id=user_id)
            session.add(identity)
            action = "создан"
    typer.echo(f"Маппинг для {normalized} {action}.")


if __name__ == "__main__":
    app()
