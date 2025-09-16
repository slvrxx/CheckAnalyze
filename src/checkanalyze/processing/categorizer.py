"""Machine learning helper for assigning categories to receipt lines."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

import numpy as np
from rapidfuzz import process
import structlog
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import LabelEncoder

from ..db import Category, CategoryTrainingSample, MerchantProfile, session_scope
from .extraction import ReceiptItemCandidate, ReceiptParseResult

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class CategorizedItem:
    description: str
    amount: float
    currency: str
    category_id: Optional[int]
    category_name: Optional[str]
    confidence: float


class Categorizer:
    """Predict categories for receipt items using labelled data and heuristics."""

    def __init__(self) -> None:
        self.pipeline: Optional[Pipeline] = None
        self.encoder = LabelEncoder()
        self._train()

    def _train(self) -> None:
        with session_scope() as session:
            samples = session.query(CategoryTrainingSample).all()
            if len(samples) < 3:
                logger.warning("insufficient_training_samples", count=len(samples))
                self.pipeline = None
                return

            texts = [self._compose_text(sample.description, sample.merchant) for sample in samples]
            labels = [sample.category_id for sample in samples]
            encoded = self.encoder.fit_transform(labels)

            self.pipeline = Pipeline(
                [
                    ("tfidf", TfidfVectorizer(ngram_range=(1, 2), max_features=5000)),
                    ("clf", LogisticRegression(max_iter=500)),
                ]
            )
            self.pipeline.fit(texts, encoded)
            logger.info("categorizer_trained", samples=len(samples), classes=len(self.encoder.classes_))

    def predict(self, parse_result: ReceiptParseResult, user_categories: Iterable[Category]) -> list[CategorizedItem]:
        merchant_name = parse_result.merchant_name or ""
        merchant_inn = parse_result.merchant_inn or ""
        items: list[CategorizedItem] = []
        for item in parse_result.items:
            category_id, confidence = self._predict_single(item, merchant_name, merchant_inn, user_categories)
            category_name = None
            if category_id is not None:
                for category in user_categories:
                    if category.id == category_id:
                        category_name = category.name
                        break
            items.append(
                CategorizedItem(
                    description=item.description,
                    amount=item.amount,
                    currency=item.currency,
                    category_id=category_id,
                    category_name=category_name,
                    confidence=confidence,
                )
            )
        return items

    def _predict_single(
        self,
        item: ReceiptItemCandidate,
        merchant_name: str,
        merchant_inn: str,
        user_categories: Iterable[Category],
    ) -> tuple[Optional[int], float]:
        text = self._compose_text(item.description, merchant_name, merchant_inn)

        if self.pipeline is not None:
            encoded = self.pipeline.predict_proba([text])[0]
            top_idx = int(np.argmax(encoded))
            confidence = float(encoded[top_idx])
            category_id = int(self.encoder.inverse_transform([top_idx])[0])
            return category_id, confidence

        # Fallback heuristics based on merchant profile hints.
        with session_scope() as session:
            merchant_profile = None
            if merchant_inn:
                merchant_profile = (
                    session.query(MerchantProfile)
                    .filter(MerchantProfile.inn == merchant_inn)
                    .first()
                )
            if not merchant_profile and merchant_name:
                merchant_profile = (
                    session.query(MerchantProfile)
                    .filter(MerchantProfile.name.ilike(f"%{merchant_name}%"))
                    .first()
                )

            if merchant_profile and merchant_profile.default_category_id:
                return merchant_profile.default_category_id, 0.55

            # Fuzzy match item description with existing category names.
            choices = {category.name: category.id for category in user_categories}
            if not choices:
                return None, 0.0
            match = process.extractOne(item.description, list(choices.keys()))
            if match and match[1] > 60:
                return choices[match[0]], match[1] / 100.0

        return None, 0.0

    def record_feedback(
        self,
        user_id: int,
        merchant_profile: Optional[MerchantProfile],
        item: ReceiptItemCandidate,
        category_id: int,
    ) -> None:
        with session_scope() as session:
            sample = CategoryTrainingSample(
                user_id=user_id,
                merchant_id=merchant_profile.id if merchant_profile else None,
                description=item.description,
                amount=item.amount,
                currency=item.currency,
                category_id=category_id,
            )
            session.add(sample)
        self._train()

    def _compose_text(
        self,
        description: str,
        merchant: Optional[MerchantProfile] | str = None,
        merchant_inn: Optional[str] = None,
    ) -> str:
        merchant_name = ""
        inn = ""
        if isinstance(merchant, MerchantProfile):
            merchant_name = merchant.name or ""
            inn = merchant.inn or ""
        elif isinstance(merchant, str):
            merchant_name = merchant
            inn = merchant_inn or ""
        parts = [description, merchant_name, inn]
        return " ".join(part for part in parts if part)


__all__ = ["Categorizer", "CategorizedItem"]
