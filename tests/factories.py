"""Builders for made-up messages. Every address is on example.com; no real data anywhere."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

from inbox_janitor.models import Attachment, Message

OWNER = "owner@harborlane.example.com"
T0 = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)

_ids = itertools.count(1)


def msg(*, ref: str | None = None, sender: str = "hello@shop.example.com", subject: str = "Hi",
        body: str = "Just saying hello.", labels=("INBOX", "UNREAD"), received=None,
        headers: dict[str, str] | None = None, attachments=(), message_id: str | None = None,
        sender_name: str = "", in_reply_to: str = "", references=(), age_days: float = 0,
        to=(OWNER,)) -> Message:
    n = next(_ids)
    ref = ref or f"m{n:04d}"
    return Message(
        ref=ref, sender=sender.lower(), sender_name=sender_name, subject=subject, body=body,
        received=received or (T0 - timedelta(days=age_days, seconds=n % 3600)),
        labels=frozenset(labels), headers={k.lower(): v for k, v in (headers or {}).items()},
        attachments=tuple(attachments), message_id=message_id or f"<{ref}@mail.example.com>",
        in_reply_to=in_reply_to, references=tuple(references), to=tuple(to))


def pdf(name: str = "invoice-1043.pdf", size: int = 48_000) -> Attachment:
    return Attachment(name=name, mime="application/pdf", size=size)


def newsletter(**kw) -> Message:
    kw.setdefault("sender", "news@flour.example.com")
    kw.setdefault("subject", "October news from the mill")
    kw.setdefault("body", "This month: new rye blends and a visit to the mill.")
    kw.setdefault("headers", {"List-Unsubscribe": "<mailto:unsub@flour.example.com>"})
    return msg(**kw)


def promotion(**kw) -> Message:
    kw.setdefault("sender", "deals@ovens.example.com")
    kw.setdefault("subject", "Flash sale: 40% off proofing baskets")
    kw.setdefault("body", "Limited time only. Shop now.")
    kw.setdefault("headers", {"List-Unsubscribe": "<https://ovens.example.com/unsub>"})
    return msg(**kw)


def invoice(**kw) -> Message:
    kw.setdefault("sender", "billing@flour.example.com")
    kw.setdefault("subject", "Invoice 1043 for September deliveries")
    kw.setdefault("body", "Please find the invoice attached. Due in 30 days.")
    kw.setdefault("attachments", (pdf(),))
    return msg(**kw)
