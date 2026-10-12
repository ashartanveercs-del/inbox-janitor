"""What the janitor may remember about a message: keyed fingerprints, never the content.

The ledger has to answer "have I seen this message / this sender / this exact content
before?" without holding subjects, addresses or bodies. A plain SHA-256 is not enough: anyone
holding the log could hash a list of likely addresses and match them. Fingerprints here are
BLAKE2b keyed with a per-install secret that never leaves the state directory, and each kind
of value gets its own domain-separation tag.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import unicodedata
from pathlib import Path

KEY_BYTES = 32
_PERSON = b"inbox-janitor"


class Fingerprinter:
    def __init__(self, key: bytes) -> None:
        if not 16 <= len(key) <= 64:
            raise ValueError("fingerprint key must be 16-64 bytes")
        self._key = bytes(key)

    def __repr__(self) -> str:
        return "Fingerprinter(<key hidden>)"

    def __call__(self, kind: str, *parts: str) -> str:
        h = hashlib.blake2b(digest_size=12, key=self._key, person=_PERSON)
        for chunk in (kind, *parts):
            data = str(chunk).encode("utf-8")
            h.update(len(data).to_bytes(4, "big"))   # length prefix: ("ab","c") != ("a","bc")
            h.update(data)
        return h.hexdigest()


def load_or_create_key(path: Path | str) -> bytes:
    """The install's secret, created once (mode 0600). A damaged file is an error, not a reason
    to mint a new key: a new key silently breaks matching against every earlier ledger row."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(KEY_BYTES)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
            fh.flush()
            os.fsync(fh.fileno())
        return key
    if len(data) != KEY_BYTES:
        raise ValueError(f"the secret key file {path} is damaged; restore it from a backup")
    return data


def normalize_text(text: str) -> str:
    """Case, Unicode width and spacing differences don't make two bodies different."""
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"\s+", " ", text).strip()


def one_line(text: str, limit: int = 80) -> str:
    """Untrusted text (a subject, a display name) made safe to show on one line: control and
    format characters become spaces, so a crafted subject can't start a new line of its own."""
    chars = (" " if c.isspace() or unicodedata.category(c).startswith("C") else c
             for c in str(text or ""))
    flat = re.sub(r" {2,}", " ", "".join(chars)).strip()
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"
