from factories import invoice, msg, newsletter, pdf

from inbox_janitor.dedupe import DuplicateIndex, fingerprints
from inbox_janitor.privacy import Fingerprinter
from inbox_janitor.storage import Ledger

FP = Fingerprinter(b"k" * 32)


def index_with(*messages) -> DuplicateIndex:
    index = DuplicateIndex()
    for ticket, m in enumerate(messages, 1):
        index.add(ticket, m.ref, fingerprints(m, FP))
    return index


def test_the_same_message_id_delivered_twice_is_a_duplicate():
    """e.g. one message sent to two of the owner's addresses."""
    first = newsletter(message_id="<abc@flour.example.com>")
    again = newsletter(message_id="<abc@flour.example.com>", body="(different footer)")
    assert index_with(first).original_of(again.ref, fingerprints(again, FP)) == 1


def test_same_sender_same_body_same_files_is_a_duplicate_even_with_a_new_message_id():
    first = invoice()
    resent = invoice(body="  please find the INVOICE attached.\n\nDue in 30 days. ")
    assert index_with(first).original_of(resent.ref, fingerprints(resent, FP)) == 1


def test_a_matching_subject_alone_never_makes_a_duplicate():
    first = msg(subject="Order update", body="Your order 1001 has shipped and is on its way.")
    second = msg(subject="Order update", body="Your order 1002 has shipped and is on its way.")
    assert index_with(first).original_of(second.ref, fingerprints(second, FP)) is None


def test_the_same_body_from_a_different_sender_is_not_a_duplicate():
    a = msg(sender="ana@example.com", body="Thanks so much for the lovely sourdough!")
    b = msg(sender="ben@example.com", body="Thanks so much for the lovely sourdough!")
    assert index_with(a).original_of(b.ref, fingerprints(b, FP)) is None


def test_a_different_attachment_is_not_a_duplicate():
    first = invoice(attachments=(pdf("invoice-1043.pdf", 48_000),))
    other = invoice(attachments=(pdf("invoice-1044.pdf", 51_200),))
    assert index_with(first).original_of(other.ref, fingerprints(other, FP)) is None


def test_short_empty_bodies_never_match_on_content():
    """Two different notifications with no body text would otherwise look identical."""
    a = msg(sender="alerts@pos.example.com", body="", subject="Till opened")
    b = msg(sender="alerts@pos.example.com", body="", subject="Till closed")
    assert fingerprints(a, FP).content is None
    assert index_with(a).original_of(b.ref, fingerprints(b, FP)) is None


def test_a_message_is_never_a_duplicate_of_itself():
    m = newsletter()
    assert index_with(m).original_of(m.ref, fingerprints(m, FP)) is None


def test_the_first_copy_stays_the_original():
    a, b, c = (newsletter(message_id="<same@flour.example.com>") for _ in range(3))
    index = index_with(a, b)
    assert index.original_of(c.ref, fingerprints(c, FP)) == 1


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
        ledger.append({"kind": "decision", "ref": m.ref, "ticket": ticket, "applied": applied,
                       "fp": {"sender": f.sender, "msgid": f.msgid, "content": f.content}})
    index = DuplicateIndex.from_ledger(ledger)
    copy_of_kept = newsletter(message_id="<k@x.example.com>")
    copy_of_trashed = newsletter(message_id="<t@x.example.com>",
                                 body="A different newsletter body for this one.")
    assert index.original_of(copy_of_kept.ref, fingerprints(copy_of_kept, FP)) == 1
    assert index.original_of(copy_of_trashed.ref, fingerprints(copy_of_trashed, FP)) is None
