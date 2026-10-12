"""Pluggable classifiers. Every answer passes through `checked`, so a classifier can only ever
say one of a fixed set of categories with a confidence in [0, 1]. It has no way to express an
action, a label or a stage: what happens to a category is the operator's policy, not the
model's choice."""

from __future__ import annotations

import math
from typing import Any, Protocol

from ..models import Classification, Message

CATEGORIES: tuple[str, ...] = (
    "invoice", "receipt", "contract", "support", "newsletter", "promotion", "notification",
    "personal", "other",
)


class Classifier(Protocol):
    name: str

    def classify(self, message: Message) -> Classification: ...


def checked(category: Any, confidence: Any, source: str) -> Classification:
    """A classifier's raw answer, or `invalid` when it is not a known category with a finite
    confidence in [0, 1]. Booleans are refused: `True` would otherwise read as 1.0."""
    if not isinstance(category, str) or category not in CATEGORIES:
        return Classification.invalid(source)
    if isinstance(confidence, bool) or not isinstance(confidence, int | float | str):
        return Classification.invalid(source)
    try:
        value = float(confidence)
    except ValueError:
        return Classification.invalid(source)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        return Classification.invalid(source)
    return Classification(category, value, source)
