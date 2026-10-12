"""The mailbox contract. The janitor only ever talks to a `Mailbox`; Gmail is one adapter, the
in-memory/JSON mailbox another (tests and the offline demo)."""

from __future__ import annotations

import email.utils
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.policy import SMTP
from typing import Protocol

from ..models import Message


class MailboxError(Exception):
    """A provider call failed. Holds the operation and status code - never a response body,
    which could echo a subject or an address into a log."""

    def __init__(self, operation: str, status: int | None = None) -> None:
        super().__init__(f"{operation} failed" + (f" (HTTP {status})" if status else ""))
        self.operation = operation
        self.status = status


class NotFound(MailboxError):
    pass


@dataclass(frozen=True)
class Outgoing:
    sender: str
    to: str
    subject: str
    body: str
    message_id: str
    in_reply_to: str = ""
    references: tuple[str, ...] = ()
    date: datetime | None = None

    def to_bytes(self) -> bytes:
        out = EmailMessage()
        out["From"] = self.sender
        out["To"] = self.to
        out["Subject"] = self.subject
        out["Date"] = email.utils.format_datetime(self.date) if self.date \
            else email.utils.formatdate()
        out["Message-ID"] = self.message_id
        if self.in_reply_to:
            out["In-Reply-To"] = self.in_reply_to
        if self.references:
            out["References"] = " ".join(self.references)
        out["Auto-Submitted"] = "auto-generated"
        out.set_content(self.body)
        return out.as_bytes(policy=SMTP)


class Mailbox(Protocol):
    serv_id: str        # the authserv-id the provider's own receiving servers stamp

    def address(self) -> str: ...

    def search(self, box: str, since: datetime) -> list[str]: ...

    def fetch(self, ref: str) -> Message: ...

    def modify(self, ref: str, add: Iterable[str] = (), remove: Iterable[str] = ()) -> None: ...

    def trash(self, ref: str) -> None: ...

    def untrash(self, ref: str) -> None: ...

    def send(self, message: Outgoing) -> str: ...
