"""Duplicate detection by keyed fingerprints.

Two messages are the same when they share a Message-ID (one message delivered to two
aliases), or when the same sender sent the same body with the same attachments within two
days (a supplier's system sending an invoice twice). Outside that window the same words are a
new message: a monthly "your statement is ready" is not last month's copy. A matching subject
alone never makes a duplicate: "Order update" from a shop says nothing about which order. A
near-empty body never matches on content, or every bodiless notification from one sender
would collapse into one.

Being a copy only changes *how* the janitor acts on a message it was going to act on anyway
(archive the copy instead); it never overrides a policy that keeps the mail.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Message
from .privacy import Fingerprinter, normalize_text
from .storage import DAY, Ledger

MIN_BODY_CHARS = 20
CONTENT_WINDOW = 2 * DAY      # seconds between a message and a same-content copy of it


@dataclass(frozen=True)
class Fingerprints:
    sender: str
    msgid: str | None
    content: str | None


def fingerprints(m: Message, fp: Fingerprinter) -> Fingerprints:
    body = normalize_text(m.body)
    files = sorted(f"{a.name.lower()}|{a.mime.lower()}|{a.size}" for a in m.attachments)
    content = None
    if len(body) >= MIN_BODY_CHARS or files:
        content = fp("content", m.sender.lower(), body, *files)
    msgid = m.message_id.strip()
    return Fingerprints(sender=fp("sender", m.sender.lower()),
                        msgid=fp("msgid", msgid) if msgid else None, content=content)


@dataclass(frozen=True)
class _Seen:
    ticket: int
    ref: str
    received: float           # epoch seconds


class DuplicateIndex:
    def __init__(self) -> None:
        self._seen: dict[tuple[str, str], list[_Seen]] = {}

    def add(self, ticket: int, ref: str, fps: Fingerprints, received: float) -> None:
        for kind, key in (("msgid", fps.msgid), ("content", fps.content)):
            if key:
                self._seen.setdefault((kind, key), []).append(_Seen(ticket, ref, received))

    def original_of(self, ref: str, fps: Fingerprints, received: float) -> int | None:
        """The ticket of the message this one is a copy of, or None. For each key, the
        original is the first message the janitor saw with it (the lowest ticket) - counting
        only those within CONTENT_WINDOW for a content match, while a Message-ID match counts
        at any distance. When that first message is this one, it is the original, not a copy.
        """
        for kind, key in (("msgid", fps.msgid), ("content", fps.content)):
            if not key:
                continue
            matches = [s for s in self._seen.get((kind, key), ())
                       if kind == "msgid" or abs(received - s.received) <= CONTENT_WINDOW]
            first = min(matches, key=lambda s: s.ticket, default=None)
            if first is not None and first.ref != ref:
                return first.ticket
        return None

    @classmethod
    def from_ledger(cls, ledger: Ledger) -> DuplicateIndex:
        """Every message the ledger knows that is still in the mailbox: anything the janitor
        trashed (and nobody restored) can't be the original of a later copy."""
        index = cls()
        for row in sorted(ledger.decisions(), key=lambda r: r.get("ticket") or 0):
            fp = row.get("fp")
            if not isinstance(fp, dict) or ["trash"] in ledger.history(row["ref"]):
                continue
            received = row.get("received")
            index.add(row["ticket"], row["ref"], Fingerprints(
                sender=fp.get("sender") or "", msgid=fp.get("msgid"), content=fp.get("content")),
                float(received) if isinstance(received, int | float) else 0.0)
        return index
