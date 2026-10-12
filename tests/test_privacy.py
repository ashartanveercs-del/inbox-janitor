import hashlib
import os
import stat

import pytest

from inbox_janitor.privacy import Fingerprinter, load_or_create_key, normalize_text, one_line

KEY = b"k" * 32


def test_a_fingerprint_is_stable_and_reveals_nothing():
    fp = Fingerprinter(KEY)
    a = fp("sender", "billing@flour.example.com")
    assert a == fp("sender", "billing@flour.example.com")
    assert len(a) == 24 and all(c in "0123456789abcdef" for c in a)
    assert "flour" not in a and "billing" not in a


def test_a_plain_hash_of_the_address_never_matches():
    """An unkeyed SHA-256 of an address can be reversed by hashing a list of known addresses.
    The keyed hash can't be recomputed without the local secret."""
    address = "billing@flour.example.com"
    plain = hashlib.sha256(address.encode()).hexdigest()
    assert Fingerprinter(KEY)("sender", address) not in plain


def test_the_key_and_the_kind_both_change_the_fingerprint():
    one, two = Fingerprinter(KEY), Fingerprinter(b"z" * 32)
    assert one("sender", "x@example.com") != two("sender", "x@example.com")
    assert one("sender", "x@example.com") != one("msgid", "x@example.com")


def test_parts_cannot_run_into_each_other():
    fp = Fingerprinter(KEY)
    assert fp("content", "ab", "c") != fp("content", "a", "bc")


def test_a_short_key_is_refused():
    with pytest.raises(ValueError):
        Fingerprinter(b"short")


def test_the_key_is_created_once_and_reused(tmp_path):
    path = tmp_path / "state" / "secret.key"
    first = load_or_create_key(path)
    assert len(first) == 32
    assert load_or_create_key(path) == first


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_the_key_file_is_private(tmp_path):
    path = tmp_path / "secret.key"
    load_or_create_key(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_damaged_key_file_is_an_error_not_a_silent_new_key(tmp_path):
    """A new key would silently break duplicate detection against every earlier row."""
    path = tmp_path / "secret.key"
    path.write_bytes(b"too short")
    with pytest.raises(ValueError, match="secret key"):
        load_or_create_key(path)


def test_one_line_flattens_control_characters():
    assert one_line("Big\r\nsale\tnever touch\x00 me") == "Big sale never touch me"


def test_one_line_truncates_with_an_ellipsis():
    assert one_line("abcdefghij", limit=6) == "abcde…"
    assert one_line("abc", limit=6) == "abc"


def test_normalize_text_ignores_case_and_spacing():
    assert normalize_text("  Hello,\n\n WORLD  ") == normalize_text("hello, world")
