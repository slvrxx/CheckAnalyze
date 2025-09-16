import email
import importlib

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool


@pytest.fixture
def app_modules(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("DEFAULT_USER_ID", "1")

    import checkanalyze.config as config
    importlib.reload(config)

    import checkanalyze.db as db
    importlib.reload(db)

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    db._engine = engine
    db.SessionFactory.configure(bind=engine)
    db.init_schema()

    import checkanalyze.service as service
    importlib.reload(service)

    import checkanalyze.email_ingest as email_ingest
    importlib.reload(email_ingest)

    return config, db, service, email_ingest


def _prepare_dummy_receipt(monkeypatch, db, service):
    from checkanalyze.processing.extraction import ReceiptParseResult, ReceiptItemCandidate
    from checkanalyze.processing.categorizer import CategorizedItem

    with db.session_scope() as session:
        user = db.User(telegram_id=12345, name="Tester")
        session.add(user)
        session.flush()
        user_id = user.id

        food_category = db.Category(name="Продукты", code="12")
        session.add(food_category)
        session.flush()
        food_category_id = food_category.id

        beer_category = db.Category(name="Пиво", code="38")
        session.add(beer_category)
        session.flush()

    parse_result = ReceiptParseResult(
        merchant_name="Магазин",
        merchant_inn="1234567890",
        currency="RUB",
        total_amount=200.0,
        items=[
            ReceiptItemCandidate(description="Продукты", amount=150.0, currency="RUB"),
            ReceiptItemCandidate(description="Пиво", amount=50.0, currency="RUB"),
        ],
    )

    predictions = [
        CategorizedItem(
            description="Продукты",
            amount=150.0,
            currency="RUB",
            category_id=food_category_id,
            category_name="Продукты",
            confidence=0.91,
        ),
        CategorizedItem(
            description="Пиво",
            amount=50.0,
            currency="RUB",
            category_id=None,
            category_name=None,
            confidence=0.22,
        ),
    ]

    monkeypatch.setattr(service, "parse_receipt", lambda data: parse_result)

    class DummyCategorizer:
        def predict(self, result, categories):
            return predictions

    monkeypatch.setattr(service, "Categorizer", lambda: DummyCategorizer())

    return user_id


def test_process_receipt_bytes_creates_summary(monkeypatch, app_modules):
    _config, db, service, _ = app_modules
    user_id = _prepare_dummy_receipt(monkeypatch, db, service)

    processed = service.process_receipt_bytes(user_id, b"pdf-data", source="telegram")

    assert processed.receipt_id > 0
    assert "Чек #" in processed.summary_text
    assert "Список категорий" in processed.summary_text

    with db.session_scope() as session:
        receipt = session.query(db.Receipt).get(processed.receipt_id)
        assert receipt is not None
        assert len(receipt.items) == 2
        assert receipt.items[0].predicted_category_id is not None


def test_set_category_and_confirm(monkeypatch, app_modules):
    _config, db, service, _ = app_modules
    user_id = _prepare_dummy_receipt(monkeypatch, db, service)

    processed = service.process_receipt_bytes(user_id, b"pdf-data", source="telegram")

    message, success = service.set_receipt_item_category(user_id, processed.receipt_id, 2, "Пиво")
    assert success
    assert "Строка 2" in message

    confirm_message, confirmed = service.confirm_receipt(user_id, processed.receipt_id)
    assert confirmed
    assert "Команды для записи" in confirm_message

    commands_block = confirm_message.split("Команды для записи:", 1)[1].strip().splitlines()
    assert commands_block[0] == "E 150 RUB 12 1 Продукты"
    assert commands_block[1] == "E 50 RUB 38 1 Пиво"

    with db.session_scope() as session:
        receipt = session.query(db.Receipt).get(processed.receipt_id)
        assert receipt.status == "confirmed"
        samples = session.query(db.CategoryTrainingSample).all()
        assert len(samples) == 2


def test_email_identity_resolution(app_modules):
    _config, db, _, email_ingest = app_modules

    with db.session_scope() as session:
        user = db.User(telegram_id=777, name="Email User")
        session.add(user)
        session.flush()
        identity = db.EmailIdentity(user_id=user.id, email="test@example.com")
        session.add(identity)

    ingestor = email_ingest.EmailIngestor()

    message = email.message_from_string("From: Test <test@example.com>\n\nBody")
    resolved_id = ingestor._resolve_user(message)

    assert resolved_id == user.id
