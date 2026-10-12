"""Sender patterns: an exact address, or a domain (with or without '@') and its subdomains."""

from __future__ import annotations

import re

_ADDRESS = re.compile(r"^[^@\s]+@[a-z0-9.-]+\.[a-z]{2,}$")
_DOMAIN = re.compile(r"^@?(?:[a-z0-9-]+\.)+[a-z]{2,}$")


def domain_of(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower() if "@" in address else ""


def is_sender_pattern(text: str) -> bool:
    text = text.strip().lower()
    return bool(_ADDRESS.match(text) or _DOMAIN.match(text))


def sender_matches(pattern: str, address: str) -> bool:
    """'a@example.com' matches only that address; '@example.com' and 'example.com' match
    example.com and its subdomains, never 'notexample.com'."""
    pattern, address = pattern.strip().lower(), address.strip().lower()
    if not pattern or not address:
        return False
    if "@" in pattern and not pattern.startswith("@"):
        return pattern == address
    domain, sender_domain = pattern.lstrip("@"), domain_of(address)
    return sender_domain == domain or sender_domain.endswith("." + domain)
