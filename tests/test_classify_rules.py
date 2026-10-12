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


GROUP = {"List-Id": "Orders <orders.harborlane.example.com>",
         "List-Post": "<mailto:orders@harborlane.example.com>",
         "List-Unsubscribe": "<mailto:orders+unsubscribe@harborlane.example.com>"}
BULKY = ("promotion", "newsletter")


def test_a_reply_in_a_list_thread_is_not_bulk_mail():
    """Google Groups and the like put list headers on every post, business threads included:
    this is a colleague, not a sale."""
    thread = msg(sender="team@harborlane.example.com",
                 subject="Re: deal with the cafe on the wholesale order",
                 headers={"List-Id": "<orders.harborlane.example.com>"})
    assert RuleClassifier().classify(thread).category not in BULKY


def test_a_list_anyone_can_post_to_is_a_discussion_not_bulk_mail():
    post = msg(sender="baker@coop.example.com", subject="Anyone have a deal on rye flour?",
               headers={**GROUP, "List-Id": "<bakers.coop.example.com>",
                        "List-Post": "<mailto:bakers@coop.example.com>"})
    assert RuleClassifier().classify(post).category not in BULKY


def test_an_announcement_list_is_still_bulk_mail():
    """RFC 2369: `List-Post: NO` means nobody can post - a one-way list."""
    sale = promotion(headers={"List-Unsubscribe": "<https://ovens.example.com/unsub>",
                              "List-Post": "NO (posting not allowed on this list)"})
    assert RuleClassifier().classify(sale).category == "promotion"


def test_list_mail_from_or_about_the_owners_own_domain_is_never_bulk():
    rules = RuleClassifier(own_domains=("harborlane.example.com",))
    sheet = msg(sender="team@harborlane.example.com", subject="Weekly order sheet",
                headers={"List-Id": "<orders.harborlane.example.com>"})
    relayed = msg(sender="noreply@lists.example.com", subject="Weekly order sheet",
                  headers={"List-Id": "<orders.harborlane.example.com>"})
    assert rules.classify(sheet).category not in BULKY
    assert rules.classify(relayed).category not in BULKY
    assert RuleClassifier().classify(sheet).category == "newsletter"     # without the owner


@pytest.mark.parametrize("message", [
    msg(sender="customer@homemail.example.com", subject="Re: your order for Saturday",
        in_reply_to="<o-311@harborlane.example.com>"),
    msg(sender="customer@homemail.example.com", subject="Can I get a receipt for my order?"),
], ids=["reply", "question"])
def test_a_reply_or_a_question_is_never_a_receipt(message):
    """A receipt is archived: a customer writing about an order must stay in the inbox."""
    assert RuleClassifier().classify(message).category != "receipt"


@pytest.mark.parametrize("subject", ["Your receipt from Corner Market", "Thanks for your order!",
                                     "Order confirmation #5512"])
def test_receipts_are_still_receipts(subject):
    assert RuleClassifier().classify(msg(subject=subject)).category == "receipt"


@pytest.mark.parametrize("subject", ["Market news: the fall bake sale is back",
                                     "Point of sale tips for the holiday rush"])
def test_the_word_sale_alone_is_not_a_reason_to_trash(subject):
    """Trash rests on unambiguous offers ('% off', 'coupon', ...), not on a word that also
    means a bake sale or a point-of-sale system."""
    got = RuleClassifier().classify(newsletter(subject=subject))
    assert got.category == "newsletter"


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
