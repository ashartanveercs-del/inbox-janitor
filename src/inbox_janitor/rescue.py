"""Spam rescue: pull a trusted sender's mail out of Spam, but only when the receiving server
vouches for who sent it.

Two things are forgeable and must not be trusted on their own:
  * the From address - anyone can write `From: billing@your-supplier`;
  * an Authentication-Results header - anyone can write one into their own message.
So a rescue needs (1) a sender on the trusted list and (2) a DMARC pass, or a DKIM pass
aligned with the From domain, in the *topmost* Authentication-Results header *stamped by the
mailbox's own receiving server*. SPF is ignored: it checks the envelope sender, not From.

The classifier never takes part: a model can be talked into calling a phishing message an
invoice, so nothing a model says moves mail out of Spam.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .addresses import domain_of, sender_matches
from .models import Message


@dataclass(frozen=True)
class RescueVerdict:
    rescue: bool
    reason: str   # "trusted" | "not_trusted" | "unauthenticated"


def _parse(value: str) -> tuple[str, list[dict[str, str]]]:
    """(authserv-id, [{method, result, property: value}]) of one Authentication-Results."""
    text = re.sub(r"\([^()]*\)", " ", value)          # drop comments
    parts = [p.strip() for p in text.split(";")]
    head = parts[0].split() if parts and parts[0] else []
    serv_id = head[0].lower() if head else ""
    results = []
    for part in parts[1:]:
        tokens = part.split()
        if not tokens or "=" not in tokens[0]:
            continue
        method, _, result = tokens[0].partition("=")
        entry = {"method": method.lower(), "result": result.lower()}
        for token in tokens[1:]:
            key, _, val = token.partition("=")
            entry[key.lower()] = val.strip('"').lower()
        results.append(entry)
    return serv_id, results


def _aligned(signing_domain: str, from_domain: str) -> bool:
    """Relaxed alignment: the same domain or a parent of it - but never a bare TLD."""
    d = signing_domain.lstrip("@").lower()
    if "." not in d:
        return False
    return from_domain == d or from_domain.endswith("." + d)


def authenticated(m: Message, serv_id: str) -> bool:
    value = m.header("authentication-results")
    if not value:
        return False
    found_id, results = _parse(value)
    if found_id != serv_id.lower():
        return False
    from_domain = domain_of(m.sender)
    for r in results:
        if r["result"] != "pass":
            continue
        if r["method"] == "dmarc" and r.get("header.from") == from_domain:
            return True
        if r["method"] == "dkim":
            signer = r.get("header.d") or domain_of(r.get("header.i", "")) \
                or r.get("header.i", "")
            if signer and _aligned(signer, from_domain):
                return True
    return False


def judge(m: Message, trusted: Iterable[str], serv_id: str,
          also_trusted: Callable[[str], bool] | None = None) -> RescueVerdict:
    """`trusted` holds the operator's patterns; `also_trusted` checks the owner's hashed
    `trust` rules."""
    listed = any(sender_matches(p, m.sender) for p in trusted)
    if not (listed or (also_trusted is not None and also_trusted(m.sender))):
        return RescueVerdict(False, "not_trusted")
    if not authenticated(m, serv_id):
        return RescueVerdict(False, "unauthenticated")
    return RescueVerdict(True, "trusted")
