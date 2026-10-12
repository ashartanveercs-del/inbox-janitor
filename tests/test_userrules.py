import pytest

from inbox_janitor.privacy import Fingerprinter
from inbox_janitor.userrules import UserRules

FP = Fingerprinter(b"k" * 32)


def rules(tmp_path) -> UserRules:
    return UserRules(tmp_path / "rules.json", FP)


def test_never_trash_can_be_scoped_to_a_category(tmp_path):
    r = rules(tmp_path)
    r.add("never_trash", "news@flour.example.com", "newsletter")
    assert r.never_trash("news@flour.example.com", "newsletter")
    assert not r.never_trash("news@flour.example.com", "promotion")
    assert not r.never_trash("other@flour.example.com", "newsletter")


def test_never_trash_without_a_category_covers_everything_from_the_sender(tmp_path):
    r = rules(tmp_path)
    r.add("never_trash", "@flour.example.com")
    assert r.never_trash("deals@mill.flour.example.com", "promotion")
    assert not r.never_trash("deals@notflour.example.com", "promotion")


def test_never_touch_and_trust(tmp_path):
    r = rules(tmp_path)
    r.add("never_touch", "accountant@books.example.com")
    r.add("trust", "flour.example.com")
    assert r.never_touch("accountant@books.example.com")
    assert not r.never_touch("someone@books.example.com")
    assert r.trusted("billing@flour.example.com")
    assert not r.trusted("billing@ovens.example.com")


def test_rules_survive_a_reload(tmp_path):
    rules(tmp_path).add("never_touch", "@books.example.com")
    assert rules(tmp_path).never_touch("ana@books.example.com")


def test_adding_the_same_rule_twice_is_a_no_op(tmp_path):
    r = rules(tmp_path)
    assert r.add("never_touch", "@books.example.com")
    assert not r.add("never_touch", "@books.example.com")
    assert len(r) == 1


def test_the_rules_file_holds_no_address_or_domain(tmp_path):
    r = rules(tmp_path)
    r.add("never_trash", "news@flour.example.com", "newsletter")
    r.add("trust", "@bank.example.com")
    text = (tmp_path / "rules.json").read_text(encoding="utf-8")
    for leak in ("flour", "news@", "bank", "example"):
        assert leak not in text


def test_a_bare_tld_never_matches_everyone(tmp_path):
    r = rules(tmp_path)
    with pytest.raises(ValueError):
        r.add("never_touch", "com")


@pytest.mark.parametrize(("kind", "pattern", "category"), [
    ("always_trash", "x@example.com", None),
    ("never_trash", "not an address", None),
    ("never_trash", "x@example.com", "memes"),
])
def test_bad_rules_are_refused(tmp_path, kind, pattern, category):
    with pytest.raises(ValueError):
        rules(tmp_path).add(kind, pattern, category)


def test_a_damaged_rules_file_is_set_aside_not_trusted(tmp_path):
    (tmp_path / "rules.json").write_text("{broken", encoding="utf-8")
    r = rules(tmp_path)
    assert len(r) == 0 and r.was_reset
