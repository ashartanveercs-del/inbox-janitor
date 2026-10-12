"""Provider-neutral data: what a mailbox adapter hands the janitor, and what a classifier says."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

# System labels shared by the adapters (Gmail's own ids; the in-memory mailbox uses the same).
INBOX, SPAM, TRASH, SENT, STARRED, UNREAD = "INBOX", "SPAM", "TRASH", "SENT", "STARRED", "UNREAD"

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Attachment:
    name: str
    mime: str
    size: int


@dataclass(frozen=True)
class Message:
    """One message as read from a mailbox. Lives in memory for one run; never persisted."""

    ref: str                                   # the provider's opaque message id
    sender: str                                # bare address, lower case
    subject: str = ""
    body: str = ""
    received: datetime = EPOCH
    labels: frozenset[str] = frozenset({INBOX})
    sender_name: str = ""
    to: tuple[str, ...] = ()
    message_id: str = ""                       # RFC 5322 Message-ID
    in_reply_to: str = ""
    references: tuple[str, ...] = ()
    headers: Mapping[str, str] = field(default_factory=dict)   # lower-case names
    attachments: tuple[Attachment, ...] = ()

    @property
    def box(self) -> str:
        if TRASH in self.labels:
            return "trash"
        if SPAM in self.labels:
            return "spam"
        return "inbox" if INBOX in self.labels else "archived"

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")


@dataclass(frozen=True)
class Classification:
    """A classifier's answer. `error` is set when there is no usable answer:
    "unavailable" (the classifier could not be reached: try again later) or "invalid" (it
    answered, but not with a known category and a confidence in [0, 1])."""

    category: str | None
    confidence: float
    source: str = ""
    error: str | None = None

    @classmethod
    def unavailable(cls, source: str) -> Classification:
        return cls(None, 0.0, source, "unavailable")

    @classmethod
    def invalid(cls, source: str) -> Classification:
        return cls(None, 0.0, source, "invalid")
