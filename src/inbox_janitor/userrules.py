"""Rules the owner adds by replying to a digest. Every kind makes the janitor *more* careful
(or, for `trust`, more protective of legitimate mail); none of them can widen what it may
destroy.

    never_touch  leave this sender's mail exactly as it is
    never_trash  never trash this sender's mail (optionally: only this category)
    trust        rescue this sender's authenticated mail from Spam

Patterns are stored as keyed fingerprints - "a:<fp>" for an address, "d:<fp>" for a domain -
so even the owner's own rules put no address on disk. Matching hashes the sender's address
and each of its parent domains (never a bare TLD) and looks those up.
"""

from __future__ import annotations

from pathlib import Path

from .addresses import domain_of, is_sender_pattern
from .classify import CATEGORIES
from .privacy import Fingerprinter
from .storage import State

KINDS = ("never_touch", "never_trash", "trust")


class UserRules:
    def __init__(self, path: Path | str, fp: Fingerprinter) -> None:
        self._state = State(path)
        self.fp = fp
        raw = self._state.get("rules")
        self._rules: list[dict[str, str | None]] = [
            r for r in (raw if isinstance(raw, list) else [])
            if isinstance(r, dict) and r.get("rule") in KINDS and isinstance(r.get("who"), str)]

    @property
    def was_reset(self) -> bool:
        return self._state.was_reset

    def __len__(self) -> int:
        return len(self._rules)

    def _who(self, pattern: str) -> str:
        p = pattern.strip().lower()
        if not is_sender_pattern(p):
            raise ValueError("expected an address like name@example.com or a domain like "
                             "@example.com")
        if "@" in p and not p.startswith("@"):
            return "a:" + self.fp("address", p)
        return "d:" + self.fp("domain", p.lstrip("@"))

    def _candidates(self, sender: str) -> set[str]:
        sender = sender.strip().lower()
        found = {"a:" + self.fp("address", sender)}
        labels = domain_of(sender).split(".")
        for i in range(len(labels) - 1):            # parent domains, never a bare TLD
            found.add("d:" + self.fp("domain", ".".join(labels[i:])))
        return found

    def add(self, kind: str, pattern: str, category: str | None = None) -> bool:
        if kind not in KINDS:
            raise ValueError(f"unknown rule {kind!r}")
        if category is not None and category not in CATEGORIES:
            raise ValueError(f"unknown category {category!r}")
        rule = {"rule": kind, "who": self._who(pattern), "category": category}
        if rule in self._rules:
            return False
        self._rules.append(rule)
        self._state["rules"] = self._rules
        self._state.save()
        return True

    def _match(self, kind: str, sender: str, category: str | None = None) -> bool:
        if not sender:
            return False
        candidates = self._candidates(sender)
        return any(r["rule"] == kind and r["who"] in candidates
                   and r.get("category") in (None, category) for r in self._rules)

    def never_touch(self, sender: str) -> bool:
        return self._match("never_touch", sender)

    def never_trash(self, sender: str, category: str | None) -> bool:
        return self._match("never_trash", sender, category)

    def trusted(self, sender: str) -> bool:
        return self._match("trust", sender)
