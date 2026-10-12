"""A deterministic classifier: ordered rules over headers, sender, subject and attachments.

Precision over recall. Anything no rule claims is "other" with low confidence, which the
confidence floor turns into "leave it alone". A rule's confidence is its author's claim, not a
measured probability: tools/eval_classifier.py measures it against labelled mail.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from functools import cached_property

from ..addresses import domain_of, sender_matches
from ..models import Classification, Message
from . import CATEGORIES, checked

UNSURE = 0.5
BULK = ("list-unsubscribe", "list-id")
_REPLY = re.compile(r"\s*re\s*:", re.I)


def _within(domain: str, parents: Collection[str]) -> bool:
    return any(domain == p or domain.endswith("." + p) for p in parents if p)


def is_reply(m: Message) -> bool:
    return bool(m.in_reply_to or m.references or _REPLY.match(m.subject))


def is_conversation(m: Message) -> bool:
    """Someone writing to the owner rather than a system notifying them: a reply in a thread,
    or a subject that asks a question ("Can I get a receipt for my order?")."""
    return is_reply(m) or m.subject.rstrip().endswith("?")


def is_bulk(m: Message, own_domains: Collection[str] = ()) -> bool:
    """One-way mailing-list mail. Discussion lists (Google Groups and the like) put the same
    list headers on every post, business threads included, so list headers alone are not
    enough: a list anyone can post to (a List-Post address; RFC 2369's `List-Post: NO` marks a
    one-way list), a reply in a thread, or a list on the owner's own domain is conversation."""
    if not any(m.header(h) for h in BULK):
        return False
    post = m.header("list-post").strip().lower()
    if post and not post.startswith("no"):
        return False
    if is_reply(m):
        return False
    list_id = m.header("list-id")
    list_domain = list_id[list_id.find("<") + 1:list_id.rfind(">")].strip().lower() \
        if "<" in list_id and ">" in list_id else list_id.strip().lower()
    return not (_within(domain_of(m.sender), own_domains)
                or _within(list_domain, own_domains))


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
    bulk: bool = False                 # only one-way list mail (see is_bulk)
    conversations: bool = True         # False: never a reply or a question (is_conversation)

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise ValueError(f"rule category {self.category!r} is not one of {CATEGORIES}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("rule confidence must be in [0, 1]")
        if not (self.senders or self.subject_any or self.body_any or self.headers_any
                or self.attachment_ext_any or self.bulk):
            raise ValueError("a rule needs at least one condition")

    @cached_property
    def _subject(self) -> re.Pattern[str] | None:
        return _term_pattern(self.subject_any)

    @cached_property
    def _body(self) -> re.Pattern[str] | None:
        return _term_pattern(self.body_any)

    def matches(self, m: Message, own_domains: Collection[str] = ()) -> bool:
        if self.bulk and not is_bulk(m, own_domains):
            return False
        if not self.conversations and is_conversation(m):
            return False
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


# Measured against eval/corpus.json by tools/eval_classifier.py. Two choices came out of it:
# a receipt is archived (it leaves the inbox), so the receipt rule never claims a customer's
# reply or question; and promotion means trash, so it rests on unambiguous offers - "sale"
# alone also means a bake sale or a point-of-sale system.
DEFAULT_RULES: tuple[Rule, ...] = (
    Rule("invoice", 0.96, subject_any=("invoice", "bill for", "statement of account"),
         attachment_ext_any=("pdf",)),
    Rule("receipt", 0.96, conversations=False,
         subject_any=("receipt", "order confirmation", "payment received",
                      "thanks for your order", "your order")),
    Rule("contract", 0.95, subject_any=("contract", "agreement", "please sign",
                                        "signature requested")),
    Rule("support", 0.95, subject_any=("support request", "ticket #", "[support]")),
    Rule("promotion", 0.97, bulk=True,
         subject_any=("% off", "deal", "deals", "coupon", "promo", "discount",
                      "limited time", "free shipping", "last chance")),
    Rule("newsletter", 0.96, bulk=True),
)


class RuleClassifier:
    name = "rules"

    def __init__(self, rules: Sequence[Rule] = DEFAULT_RULES, *,
                 own_domains: Collection[str] = ()) -> None:
        """`own_domains`: the owner's domains, whose list mail is never treated as bulk."""
        self.rules = tuple(rules)
        self.own_domains = tuple(d.strip().lower().lstrip("@") for d in own_domains)

    def classify(self, message: Message) -> Classification:
        for rule in self.rules:
            if rule.matches(message, self.own_domains):
                return checked(rule.category, rule.confidence, self.name)
        return checked("other", UNSURE, self.name)
