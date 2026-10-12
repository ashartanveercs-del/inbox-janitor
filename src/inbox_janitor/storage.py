"""The ledger (append-only JSONL) and the run state (one JSON object), both crash-tolerant.

Ledger rows hold provider message ids, ticket numbers, keyed fingerprints, categories,
confidences and outcome codes - never subjects, addresses or bodies. Row kinds:

    start     written before a message is worked on
    decision  what was decided for it and which changes were applied
    error     working on it raised (only the exception's type is kept)
    undo      the owner reversed it; it is final from then on
    keep      the owner settled it as it is; final
    reply     a reply-command message was processed (by fingerprint)

A `start` with no `decision` or `error` after it means a run died on that message, so a
message that crashes the process can't block every later run. Every row carries `ts`, the
epoch second it was written; retention and torn-write repair both key off it.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

DAY = 86400.0
STAMP = "ts"


def append_private(path: Path, text: str) -> None:
    """Add `text` at the end of a file only its owner can read (mode 0600), and make it durable
    before returning: once a row is reported written, a crash can't take it back."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    with open(handle, "a", encoding="utf-8", newline="\n") as out:
        out.write(text)
        out.flush()
        os.fsync(out.fileno())


def _encode(row: Mapping[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"


def _live(line: str, oldest: float) -> dict[str, Any] | None:
    """One stored line as a row, or None when it is torn, isn't a row, or has expired."""
    try:
        row = json.loads(line)
    except ValueError:
        return None
    if not isinstance(row, dict):
        return None
    stamp = row.get(STAMP)
    if isinstance(stamp, bool) or not isinstance(stamp, int | float):
        return None
    return row if stamp >= oldest else None


def atomic_write_text(path: Path, text: str) -> None:
    """Write-then-rename, so a crash leaves either the old file or the new one."""
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


class Ledger:
    def __init__(self, path: Path | str, *, now: Callable[[], float] = time.time,
                 keep_days: int = 180) -> None:
        self.path = Path(path)
        self.now = now
        self.rows: list[dict[str, Any]] = []
        self._latest: dict[str, dict[str, Any]] = {}
        self._tickets: dict[int, str] = {}
        self._ticket_of: dict[str, int] = {}
        self._open: set[str] = set()
        self._errors: dict[str, int] = {}
        self._final: set[str] = set()
        self._undone: set[str] = set()
        self._history: dict[str, list[list[str]]] = {}
        self._replies: set[str] = set()
        self._load(keep_days)

    def _load(self, keep_days: int) -> None:
        try:
            stored = self.path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return
        oldest = self.now() - keep_days * DAY
        lines = [line for line in stored.splitlines() if line.strip()]
        live = [row for row in (_live(line, oldest) for line in lines) if row is not None]
        for row in live:
            self._remember(row)
        # The file is rewritten only when something has to go: an expired or damaged row, or
        # a last line that a dying run never finished (no newline after it).
        unfinished = bool(stored) and not stored.endswith("\n")
        if len(live) < len(lines) or unfinished:
            atomic_write_text(self.path, "".join(_encode(row) for row in live))

    def _remember(self, row: dict[str, Any]) -> None:
        self.rows.append(row)
        self._index(row)

    def _index(self, row: dict[str, Any]) -> None:
        kind, ref, ticket = row.get("kind"), row.get("ref"), row.get("ticket")
        if ref and isinstance(ticket, int):
            self._tickets[ticket] = ref
            self._ticket_of[ref] = ticket
        if ref is None and isinstance(ticket, int):
            ref = self._tickets.get(ticket)
        if kind == "reply":
            self._replies.add(str(row.get("fp")))
        if ref is None:
            return
        if kind == "start":
            self._open.add(ref)
        elif kind == "error":
            self._open.discard(ref)
            self._errors[ref] = self._errors.get(ref, 0) + 1
        elif kind == "decision":
            self._open.discard(ref)
            self._errors.pop(ref, None)
            self._latest[ref] = row
            self._history.setdefault(ref, []).extend(row.get("applied") or [])
        elif kind == "undo":
            self._final.add(ref)
            if row.get("ok"):
                self._undone.add(ref)
                self._history[ref] = []
        elif kind == "keep":
            self._final.add(ref)

    def append(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Stamp the row (any stamp it already had is replaced), write it durably, index it."""
        entry = dict(row)
        entry[STAMP] = round(self.now(), 3)
        append_private(self.path, _encode(entry))
        self._remember(entry)
        return entry

    # -- queries ----------------------------------------------------------------------------

    def decision(self, ref: str) -> dict[str, Any] | None:
        return self._latest.get(ref)

    def decisions(self) -> list[dict[str, Any]]:
        return list(self._latest.values())

    def ref_of(self, ticket: int) -> str | None:
        return self._tickets.get(ticket)

    def ticket_of(self, ref: str) -> int | None:
        return self._ticket_of.get(ref)

    def max_ticket(self) -> int:
        return max(self._tickets, default=0)

    def cut_off(self, ref: str) -> bool:
        return ref in self._open

    def failures(self, ref: str) -> int:
        return self._errors.get(ref, 0)

    def history(self, ref: str) -> list[list[str]]:
        return list(self._history.get(ref, []))

    def is_final(self, ref: str) -> bool:
        return ref in self._final

    def undone(self, ref: str) -> bool:
        return ref in self._undone

    def seen_reply(self, fp: str) -> bool:
        return fp in self._replies


class State(dict):
    """state.json: counters and bookkeeping. A damaged file is set aside (kept for a human to
    look at) and a fresh state is started; ticket numbers still continue from the ledger."""

    def __init__(self, path: Path | str) -> None:
        super().__init__()
        self.path = Path(path)
        self.was_reset = False
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("state is not an object")
        except ValueError:
            os.replace(self.path, self.path.with_name(f"{self.path.name}.bad-{int(time.time())}"))
            self.was_reset = True
            return
        self.update(data)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, json.dumps(self, indent=1, sort_keys=True) + "\n")
