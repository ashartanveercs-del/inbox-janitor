"""Duplicate detection by keyed fingerprints.

Two messages are the same when they share a Message-ID (one message delivered to two
aliases), or when the same sender sent the same body with the same attachments (a supplier's
system sending an invoice twice). A matching subject alone never makes a duplicate: "Order
update" from a shop says nothing about which order. A near-empty body never matches on
content, or every bodiless notification from one sender would collapse into one.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Message
from .privacy import Fingerprinter, normalize_text
from .storage import Ledger

MIN_BODY_CHARS = 20


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


class DuplicateIndex:
    def __init__(self) -> None:
        self._seen: dict[tuple[str, str], tuple[int, str]] = {}

    def add(self, ticket: int, ref: str, fps: Fingerprints) -> None:
        for kind, key in (("msgid", fps.msgid), ("content", fps.content)):
            if key:
                self._seen.setdefault((kind, key), (ticket, ref))   # the first copy stays

    def original_of(self, ref: str, fps: Fingerprints) -> int | None:
        for kind, key in (("msgid", fps.msgid), ("content", fps.content)):
            hit = self._seen.get((kind, key)) if key else None
            if hit and hit[1] != ref:
                return hit[0]
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
            index.add(row["ticket"], row["ref"], Fingerprints(
                sender=fp.get("sender") or "", msgid=fp.get("msgid"), content=fp.get("content")))
        return index
