"""A deterministic classifier: ordered rules over headers, sender, subject and attachments.

Precision over recall. Anything no rule claims is "other" with low confidence, which the
confidence floor turns into "leave it alone".
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cached_property

from ..addresses import sender_matches
from ..models import Classification, Message
from . import CATEGORIES, checked

UNSURE = 0.5


def _term_pattern(terms: Sequence[str]) -> re.Pattern[str] | None:
    """Whole-word matching ('sale' is not in 'wholesale'); a term that starts or ends with a
    symbol ('% off') only needs a boundary on its word side."""
    if not terms:
        return None
    parts = []
    for term in terms:
        left = r"\b" if term[:1].isalnum() else ""
        right = r"\b" if term[-1:].isalnum() else ""
        parts.append(left + re.escape(term.lower()) + right)
    return re.compile("|".join(parts))


@dataclass(frozen=True)
class Rule:
    """Every condition given must hold; within one condition, any term will do."""

    category: str
    confidence: float
    senders: tuple[str, ...] = ()
    subject_any: tuple[str, ...] = ()
    body_any: tuple[str, ...] = ()
    headers_any: tuple[str, ...] = ()
    attachment_ext_any: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise ValueError(f"rule category {self.category!r} is not one of {CATEGORIES}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("rule confidence must be in [0, 1]")
        if not (self.senders or self.subject_any or self.body_any or self.headers_any
                or self.attachment_ext_any):
            raise ValueError("a rule needs at least one condition")

    @cached_property
    def _subject(self) -> re.Pattern[str] | None:
        return _term_pattern(self.subject_any)

    @cached_property
    def _body(self) -> re.Pattern[str] | None:
        return _term_pattern(self.body_any)

    def matches(self, m: Message) -> bool:
        if self.senders and not any(sender_matches(p, m.sender) for p in self.senders):
            return False
        if self._subject and not self._subject.search(m.subject.lower()):
            return False
        if self._body and not self._body.search(m.body.lower()):
            return False
        if self.headers_any and not any(m.header(h) for h in self.headers_any):
            return False
        if self.attachment_ext_any:
            exts = {a.name.rsplit(".", 1)[-1].lower() for a in m.attachments if "." in a.name}
            if not exts & {e.lower().lstrip(".") for e in self.attachment_ext_any}:
                return False
        return True


BULK = ("list-unsubscribe", "list-id")

DEFAULT_RULES: tuple[Rule, ...] = (
    Rule("invoice", 0.96, subject_any=("invoice", "bill for", "statement of account"),
         attachment_ext_any=("pdf",)),
    Rule("receipt", 0.96, subject_any=("receipt", "order confirmation", "payment received",
                                       "thanks for your order", "your order")),
    Rule("contract", 0.95, subject_any=("contract", "agreement", "please sign",
                                        "signature requested")),
    Rule("support", 0.95, subject_any=("support request", "ticket #", "[support]")),
    Rule("promotion", 0.97, headers_any=BULK,
         subject_any=("% off", "sale", "deal", "deals", "coupon", "promo", "discount",
                      "limited time", "free shipping", "last chance")),
    Rule("newsletter", 0.96, headers_any=BULK),
)


class RuleClassifier:
    name = "rules"

    def __init__(self, rules: Sequence[Rule] = DEFAULT_RULES) -> None:
        self.rules = tuple(rules)

    def classify(self, message: Message) -> Classification:
        for rule in self.rules:
            if rule.matches(message):
                return checked(rule.category, rule.confidence, self.name)
        return checked("other", UNSURE, self.name)
