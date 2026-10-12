import math

import pytest
from factories import invoice, msg, newsletter, pdf, promotion

from inbox_janitor.classify import CATEGORIES, checked
from inbox_janitor.classify.rules import DEFAULT_RULES, Rule, RuleClassifier
from inbox_janitor.models import Classification


@pytest.mark.parametrize(
    ("message", "category"),
    [
        (promotion(), "promotion"),
        (newsletter(), "newsletter"),
        (invoice(), "invoice"),
        (msg(subject="Your receipt from Corner Market", sender="receipts@market.example.com"),
         "receipt"),
        (msg(subject="Support request #5521: oven door hinge"), "support"),
        (msg(subject="Please sign: lease agreement renewal"), "contract"),
    ],
)
def test_default_rules_recognise_common_mail(message, category):
    got = RuleClassifier().classify(message)
    assert got.category == category
    assert got.confidence >= 0.95
    assert got.source == "rules"


def test_unrecognised_mail_is_other_and_unsure():
    got = RuleClassifier().classify(msg(subject="Lunch on Thursday?", body="Are you free?"))
    assert got.category == "other"
    assert got.confidence < 0.95


def test_an_invoice_needs_the_word_and_a_document():
    no_file = invoice(attachments=())
    assert RuleClassifier().classify(no_file).category != "invoice"


def test_a_promotion_needs_bulk_headers_not_just_a_salesy_subject():
    personal = msg(subject="Garage sale this weekend", sender="neighbour@example.com")
    assert RuleClassifier().classify(personal).category != "promotion"


def test_operator_rules_go_first():
    rules = [Rule(category="receipt", confidence=0.99, senders=("@pos.example.com",)),
             *DEFAULT_RULES]
    got = RuleClassifier(rules).classify(promotion(sender="till@pos.example.com"))
    assert (got.category, got.confidence) == ("receipt", 0.99)


@pytest.mark.parametrize(("pattern", "sender", "hit"), [
    ("billing@flour.example.com", "billing@flour.example.com", True),
    ("billing@flour.example.com", "xbilling@flour.example.com", False),
    ("@flour.example.com", "anyone@flour.example.com", True),
    ("@flour.example.com", "anyone@mill.flour.example.com", True),
    ("@flour.example.com", "anyone@notflour.example.com", False),
    ("flour.example.com", "anyone@flour.example.com", True),
])
def test_sender_patterns(pattern, sender, hit):
    rule = Rule(category="receipt", confidence=0.99, senders=(pattern,))
    assert rule.matches(msg(sender=sender)) is hit


def test_attachment_extension_matching_ignores_case():
    rule = Rule(category="invoice", confidence=0.96, attachment_ext_any=("pdf",))
    assert rule.matches(msg(attachments=(pdf(name="INV-7.PDF"),)))


def test_a_rule_with_no_conditions_is_refused():
    with pytest.raises(ValueError, match="condition"):
        Rule(category="promotion", confidence=0.99)


def test_a_rule_with_an_unknown_category_is_refused():
    with pytest.raises(ValueError, match="category"):
        Rule(category="delete_everything", confidence=0.99, senders=("@x.example.com",))


# -- the shared output check every classifier goes through --------------------------------

def test_checked_passes_valid_answers():
    assert checked("invoice", 0.97, "llm") == Classification("invoice", 0.97, "llm")


@pytest.mark.parametrize("confidence", [1.01, -0.1, math.nan, math.inf, "high", None, True])
def test_checked_turns_a_bad_confidence_into_an_invalid_answer(confidence):
    got = checked("invoice", confidence, "llm")
    assert got.category is None and got.confidence == 0.0 and got.error == "invalid"


def test_checked_accepts_a_numeric_string():
    assert checked("receipt", "0.96", "llm").confidence == 0.96


@pytest.mark.parametrize("category", ["", "Invoice!", "trash", None, 3])
def test_checked_refuses_categories_outside_the_fixed_set(category):
    got = checked(category, 0.99, "llm")
    assert got.category is None and got.error == "invalid"


def test_categories_are_a_fixed_tuple():
    assert isinstance(CATEGORIES, tuple)
    assert "promotion" in CATEGORIES and "personal" in CATEGORIES
