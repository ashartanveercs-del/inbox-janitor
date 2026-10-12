"""An in-memory mailbox with Gmail's label semantics, and a JSON-file-backed variant for the
offline demo (`--mailbox json:PATH`). Every write is recorded in `writes`."""

from __future__ import annotations

import dataclasses
import email.utils
import itertools
import json
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..models import INBOX, SENT, SPAM, TRASH, UNREAD, Attachment, Message
from ..storage import atomic_write_text
from . import NotFound, Outgoing

BOX_LABEL = {"inbox": INBOX, "spam": SPAM, "sent": SENT}


class MemoryMailbox:
    def __init__(self, owner: str, messages: Iterable[Message] = (), *,
                 serv_id: str = "mx.example.com",
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.owner = owner.lower()
        self.serv_id = serv_id
        self.now = now
        self.messages: dict[str, Message] = {}
        self.writes: list[tuple[Any, ...]] = []
        self.sent: list[Outgoing] = []
        self._ids = itertools.count(1)
        for m in messages:
            self.add(m)

    def add(self, m: Message) -> str:
        self.messages[m.ref] = m
        return m.ref

    def _get(self, ref: str) -> Message:
        try:
            return self.messages[ref]
        except KeyError:
            raise NotFound("fetch", 404) from None

    def _relabel(self, ref: str, labels: frozenset[str]) -> None:
        self.messages[ref] = dataclasses.replace(self._get(ref), labels=labels)

    def _changed(self) -> None:
        """Hook for persistent subclasses."""

    # -- the Mailbox contract ---------------------------------------------------------------

    def address(self) -> str:
        return self.owner

    def search(self, box: str, since: datetime) -> list[str]:
        want = BOX_LABEL[box]
        found = [m for m in self.messages.values()
                 if want in m.labels and m.received >= since
                 and not (m.labels & ({TRASH} if box == "spam" else {TRASH, SPAM}))]
        return [m.ref for m in sorted(found, key=lambda m: m.received, reverse=True)]

    def fetch(self, ref: str) -> Message:
        return self._get(ref)

    def modify(self, ref: str, add: Iterable[str] = (), remove: Iterable[str] = ()) -> None:
        add, remove = tuple(add), tuple(remove)
        self._relabel(ref, (self._get(ref).labels | set(add)) - set(remove))
        self.writes.append(("modify", ref, add, remove))
        self._changed()

    def trash(self, ref: str) -> None:
        self._relabel(ref, (self._get(ref).labels - {INBOX}) | {TRASH})
        self.writes.append(("trash", ref))
        self._changed()

    def untrash(self, ref: str) -> None:
        self._relabel(ref, self._get(ref).labels - {TRASH})
        self.writes.append(("untrash", ref))
        self._changed()

    def send(self, message: Outgoing) -> str:
        ref = f"sent{next(self._ids)}"
        labels = {SENT} | ({INBOX, UNREAD} if message.to.lower() == self.owner else set())
        self.messages[ref] = Message(
            ref=ref, sender=message.sender.lower(), subject=message.subject,
            body=message.body, received=message.date or self.now(),
            labels=frozenset(labels), to=(message.to.lower(),),
            message_id=message.message_id, in_reply_to=message.in_reply_to,
            references=message.references, headers={"auto-submitted": "auto-generated"})
        self.sent.append(message)
        self.writes.append(("send", ref))
        self._changed()
        return ref


# -- the JSON file format used by the demo ---------------------------------------------------

_RELATIVE = re.compile(r"^-(\d+(?:\.\d+)?)([mhd])$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def _when(value: Any, now: datetime) -> datetime:
    """An ISO time, or an age like "-2h" / "-3d" (so a demo mailbox never goes stale)."""
    text = str(value).strip()
    relative = _RELATIVE.match(text)
    if relative:
        return now - timedelta(**{_UNITS[relative.group(2)]: float(relative.group(1))})
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def message_from_json(d: dict[str, Any], now: datetime | None = None) -> Message:
    name, address = email.utils.parseaddr(str(d.get("from", "")))
    to = d.get("to", [])
    return Message(
        ref=str(d["ref"]), sender=address.lower(), sender_name=name,
        subject=str(d.get("subject", "")), body=str(d.get("body", "")),
        received=_when(d["received"], now or datetime.now(UTC)),
        labels=frozenset(d.get("labels", [INBOX])),
        to=tuple([to] if isinstance(to, str) else to),
        message_id=str(d.get("message_id") or f"<{d['ref']}@demo.example.com>"),
        in_reply_to=str(d.get("in_reply_to", "")), references=tuple(d.get("references", ())),
        headers={str(k).lower(): str(v) for k, v in (d.get("headers") or {}).items()},
        attachments=tuple(Attachment(str(a["name"]), str(a.get("mime", "")), int(a["size"]))
                          for a in d.get("attachments", ())))


def message_to_json(m: Message) -> dict[str, Any]:
    return {
        "ref": m.ref, "from": email.utils.formataddr((m.sender_name, m.sender)),
        "to": list(m.to), "subject": m.subject, "body": m.body,
        "received": m.received.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "labels": sorted(m.labels), "headers": dict(m.headers),
        "attachments": [dataclasses.asdict(a) for a in m.attachments],
        "message_id": m.message_id, "in_reply_to": m.in_reply_to,
        "references": list(m.references),
    }


class JsonFileMailbox(MemoryMailbox):
    def __init__(self, path: Path | str, *,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.path = Path(path)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self._loading = True
        loaded_at = now()
        super().__init__(data["owner"],
                         (message_from_json(d, loaded_at) for d in data["messages"]),
                         serv_id=data.get("serv_id", "mx.example.com"), now=now)
        sent = [r for r in self.messages if r.startswith("sent")]
        self._ids = itertools.count(len(sent) + 1)
        self._loading = False

    def _changed(self) -> None:
        if self._loading:
            return
        data = {"owner": self.owner, "serv_id": self.serv_id,
                "messages": [message_to_json(m) for m in self.messages.values()]}
        atomic_write_text(self.path, json.dumps(data, indent=2) + "\n")
