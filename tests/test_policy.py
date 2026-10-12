from datetime import date

import pytest
from factories import OWNER, msg, newsletter, promotion

from inbox_janitor.config import DUPLICATE_LABEL, Config
from inbox_janitor.models import Classification
from inbox_janitor.policy import decide, duplicate_verdict, exemption, gate
from inbox_janitor.privacy import Fingerprinter
from inbox_janitor.stages import Action, Stage
from inbox_janitor.userrules import UserRules

CONFIG = Config.from_dict({"owner": OWNER})
OWNERS = frozenset({OWNER})


def c(category, confidence, source="rules"):
    return Classification(category, confidence, source)


@pytest.fixture
def rules(tmp_path):
    return UserRules(tmp_path / "rules.json", Fingerprinter(b"k" * 32))


def test_a_confident_answer_gets_the_policy_action_and_label():
    v = decide(c("newsletter", 0.97), newsletter(), config=CONFIG, rules=None)
    assert (v.outcome, v.action, v.label, v.reason) == ("act", Action.ARCHIVE,
                                                        "Janitor/Newsletters", "policy")


def test_the_floor_is_inclusive_at_095():
    assert decide(c("newsletter", 0.95), newsletter(), config=CONFIG, rules=None).outcome == "act"
    below = decide(c("newsletter", 0.949), newsletter(), config=CONFIG, rules=None)
    assert (below.outcome, below.action, below.reason) == ("review", Action.KEEP,
                                                           "low_confidence")


def test_a_raised_floor_is_honoured():
    strict = Config.from_dict({"owner": OWNER, "min_confidence": 0.98})
    assert decide(c("newsletter", 0.97), newsletter(), config=strict, rules=None).outcome \
        == "review"


def test_an_invalid_answer_is_reviewed_not_guessed():
    v = decide(Classification.invalid("llm"), newsletter(), config=CONFIG, rules=None)
    assert (v.outcome, v.reason) == ("review", "invalid_answer")


def test_an_unavailable_classifier_means_wait_then_review():
    down = Classification.unavailable("llm")
    assert decide(down, msg(), config=CONFIG, rules=None).outcome == "wait"
    late = decide(down, msg(), config=CONFIG, rules=None, waited_too_long=True)
    assert (late.outcome, late.reason) == ("review", "classifier_unavailable")


@pytest.mark.parametrize("confidence", [0.99, 0.3])
def test_keep_categories_are_kept_whatever_the_confidence(confidence):
    v = decide(c("personal", confidence), msg(), config=CONFIG, rules=None)
    assert (v.outcome, v.action) == ("kept", Action.KEEP)


def test_never_trash_turns_trash_into_archive(rules):
    rules.add("never_trash", "@ovens.example.com")
    v = decide(c("promotion", 0.99), promotion(), config=CONFIG, rules=rules)
    assert (v.outcome, v.action, v.label, v.reason) == ("act", Action.ARCHIVE,
                                                        "Janitor/Promotions", "never_trash")


def test_a_never_trash_rule_for_another_category_does_not_apply(rules):
    rules.add("never_trash", "@ovens.example.com", "newsletter")
    v = decide(c("promotion", 0.99), promotion(), config=CONFIG, rules=rules)
    assert v.action is Action.TRASH


def test_the_verdict_carries_the_answer_for_the_ledger():
    v = decide(c("newsletter", 0.97, "llm"), newsletter(), config=CONFIG, rules=None)
    assert (v.category, v.confidence, v.source) == ("newsletter", 0.97, "llm")


# -- exemptions: checked before any classifier is asked ------------------------------------

def test_the_owners_own_mail_is_exempt():
    assert exemption(msg(sender=OWNER), owners=OWNERS, rules=None) == "own_mail"
    assert exemption(msg(labels=("SENT", "INBOX")), owners=OWNERS, rules=None) == "own_mail"


def test_starred_mail_is_exempt():
    assert exemption(msg(labels=("INBOX", "STARRED")), owners=OWNERS, rules=None) == "starred"


def test_never_touch_is_exempt(rules):
    rules.add("never_touch", "accountant@books.example.com")
    m = msg(sender="accountant@books.example.com")
    assert exemption(m, owners=OWNERS, rules=rules) == "never_touch"


def test_ordinary_mail_is_not_exempt(rules):
    assert exemption(newsletter(), owners=OWNERS, rules=rules) is None


def test_a_duplicate_is_archived_under_its_own_label():
    v = duplicate_verdict(7)
    assert (v.outcome, v.action, v.label, v.reason) == ("act", Action.ARCHIVE, DUPLICATE_LABEL,
                                                        "duplicate_of_7")


# -- the gate: stage, backlog and caps -----------------------------------------------------

DAY = date(2026, 10, 12)


def g(action, stage, received=DAY, since=DAY, room=5):
    return gate(action, stage=stage, received_day=received, trash_since=since, trash_room=room)


def test_trial_only_applies_rescue():
    assert g(Action.RESCUE, Stage.TRIAL) == "apply"
    assert g(Action.ARCHIVE, Stage.TRIAL) == "suggested"
    assert g(Action.TRASH, Stage.TRIAL) == "suggested"


def test_file_stage_files_but_does_not_trash():
    assert g(Action.ARCHIVE, Stage.FILE) == "apply"
    assert g(Action.LABEL, Stage.FILE) == "apply"
    assert g(Action.TRASH, Stage.FILE) == "suggested"


def test_trash_stage_trashes_only_mail_from_after_trash_was_enabled():
    assert g(Action.TRASH, Stage.TRASH, received=DAY) == "apply"
    assert g(Action.TRASH, Stage.TRASH, received=date(2026, 10, 11)) == "backlog"
    assert g(Action.TRASH, Stage.TRASH, since=None) == "backlog"


def test_trash_stops_at_the_cap():
    assert g(Action.TRASH, Stage.TRASH, room=0) == "capped"
    assert g(Action.ARCHIVE, Stage.TRASH, room=0) == "apply"
