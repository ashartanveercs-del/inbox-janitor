from factories import invoice, msg, newsletter, pdf

from inbox_janitor.dedupe import DuplicateIndex, fingerprints
from inbox_janitor.privacy import Fingerprinter
from inbox_janitor.storage import Ledger

FP = Fingerprinter(b"k" * 32)


def index_with(*messages) -> DuplicateIndex:
    index = DuplicateIndex()
    for ticket, m in enumerate(messages, 1):
        index.add(ticket, m.ref, fingerprints(m, FP), m.received.timestamp())
    return index


def original(index: DuplicateIndex, m, received=None):
    when = m.received.timestamp() if received is None else received
    return index.original_of(m.ref, fingerprints(m, FP), when)


def test_the_same_message_id_delivered_twice_is_a_duplicate():
    """e.g. one message sent to two of the owner's addresses."""
    first = newsletter(message_id="<abc@flour.example.com>")
    again = newsletter(message_id="<abc@flour.example.com>", body="(different footer)")
    assert original(index_with(first), again) == 1


def test_same_sender_same_body_same_files_is_a_duplicate_even_with_a_new_message_id():
    first = invoice()
    resent = invoice(body="  please find the INVOICE attached.\n\nDue in 30 days. ")
    assert original(index_with(first), resent) == 1


def test_a_matching_subject_alone_never_makes_a_duplicate():
    first = msg(subject="Order update", body="Your order 1001 has shipped and is on its way.")
    second = msg(subject="Order update", body="Your order 1002 has shipped and is on its way.")
    assert original(index_with(first), second) is None


def test_the_same_body_from_a_different_sender_is_not_a_duplicate():
    a = msg(sender="ana@example.com", body="Thanks so much for the lovely sourdough!")
    b = msg(sender="ben@example.com", body="Thanks so much for the lovely sourdough!")
    assert original(index_with(a), b) is None


def test_a_different_attachment_is_not_a_duplicate():
    first = invoice(attachments=(pdf("invoice-1043.pdf", 48_000),))
    other = invoice(attachments=(pdf("invoice-1044.pdf", 51_200),))
    assert original(index_with(first), other) is None


def test_short_empty_bodies_never_match_on_content():
    """Two different notifications with no body text would otherwise look identical."""
    a = msg(sender="alerts@pos.example.com", body="", subject="Till opened")
    b = msg(sender="alerts@pos.example.com", body="", subject="Till closed")
    assert fingerprints(a, FP).content is None
    assert original(index_with(a), b) is None


def test_a_message_is_never_a_duplicate_of_itself():
    m = newsletter()
    assert original(index_with(m), m) is None


def test_the_first_copy_stays_the_original():
    a, b, c = (newsletter(message_id="<same@flour.example.com>") for _ in range(3))
    index = index_with(a, b)
    assert original(index, c) == 1


def test_the_original_is_never_a_copy_of_a_later_copy():
    """Read again on a later run (a suggestion acted on once the stage allows it), the first
    copy is still the original, not a copy of the copy that came after it."""
    first, copy = invoice(), invoice()
    index = index_with(first, copy)
    assert original(index, first) is None and original(index, copy) == 1


DAY = 86400.0


def test_content_only_matches_within_two_days():
    """A re-send arrives within hours; a template reused next month is a new message."""
    first = invoice()
    resent = invoice(body=first.body)
    t = first.received.timestamp()
    index = index_with(first)
    assert original(index, resent, t + 47 * 3600) == 1
    assert original(index, resent, t + 49 * 3600) is None
    assert original(index, resent, t - 49 * 3600) is None          # in either direction


def test_a_message_id_match_has_no_window():
    first = newsletter(message_id="<issue-41@flour.example.com>")
    again = newsletter(message_id="<issue-41@flour.example.com>")
    assert original(index_with(first), again, first.received.timestamp() + 30 * DAY) == 1


def test_the_original_is_the_first_copy_inside_the_window():
    """Last month's statement can't be the original of this month's re-send: this month's
    first copy is."""
    last_month, this_month, resent = invoice(), invoice(), invoice()
    t = last_month.received.timestamp()
    index = DuplicateIndex()
    index.add(1, last_month.ref, fingerprints(last_month, FP), t)
    index.add(2, this_month.ref, fingerprints(this_month, FP), t + 30 * DAY)
    assert original(index, resent, t + 30 * DAY + 600) == 2


def test_fingerprints_hold_no_content():
    m = invoice()
    text = repr(fingerprints(m, FP))
    for leak in ("flour", "invoice", "billing", "1043"):
        assert leak not in text.lower()


def test_the_index_is_rebuilt_from_the_ledger_but_skips_what_was_trashed(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", now=lambda: 1_760_000_000.0)
    kept, trashed = newsletter(message_id="<k@x.example.com>"), newsletter(
        message_id="<t@x.example.com>", body="A different newsletter body for this one.")
    for ticket, (m, applied) in enumerate([(kept, []), (trashed, [["trash"]])], 1):
        f = fingerprints(m, FP)
        if applied:
            ledger.append({"kind": "intent", "ref": m.ref, "ticket": ticket, "ops": applied})
        ledger.append({"kind": "decision", "ref": m.ref, "ticket": ticket, "applied": applied,
                       "fp": {"sender": f.sender, "msgid": f.msgid, "content": f.content}})
    index = DuplicateIndex.from_ledger(ledger)
    copy_of_kept = newsletter(message_id="<k@x.example.com>")
    copy_of_trashed = newsletter(message_id="<t@x.example.com>",
                                 body="A different newsletter body for this one.")
    assert original(index, copy_of_kept) == 1
    assert original(index, copy_of_trashed) is None
