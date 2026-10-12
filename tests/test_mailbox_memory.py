import email
import json
from datetime import timedelta

import pytest
from factories import OWNER, T0, msg, newsletter

from inbox_janitor.mailbox import NotFound, Outgoing
from inbox_janitor.mailbox.memory import JsonFileMailbox, MemoryMailbox


def box(*messages) -> MemoryMailbox:
    mb = MemoryMailbox(OWNER)
    for m in messages:
        mb.add(m)
    return mb


def test_search_filters_by_box_and_age():
    fresh, old = newsletter(age_days=1), newsletter(age_days=40)
    junk = msg(labels=("SPAM",), age_days=2)
    mb = box(fresh, old, junk)
    since = T0 - timedelta(days=30)
    assert mb.search("inbox", since) == [fresh.ref]
    assert mb.search("spam", since) == [junk.ref]


def test_trashed_mail_is_in_no_box():
    m = newsletter()
    mb = box(m)
    mb.trash(m.ref)
    assert mb.search("inbox", T0 - timedelta(days=30)) == []
    assert mb.fetch(m.ref).box == "trash"


def test_untrash_takes_it_out_of_the_trash():
    m = newsletter()
    mb = box(m)
    mb.trash(m.ref)
    mb.untrash(m.ref)
    assert "TRASH" not in mb.fetch(m.ref).labels


def test_modify_adds_and_removes_labels_and_records_the_write():
    m = newsletter()
    mb = box(m)
    mb.modify(m.ref, add=["Janitor/Newsletters"], remove=["INBOX"])
    assert mb.fetch(m.ref).labels == {"UNREAD", "Janitor/Newsletters"}
    assert mb.writes == [("modify", m.ref, ("Janitor/Newsletters",), ("INBOX",))]


def test_an_unknown_message_is_not_found():
    with pytest.raises(NotFound):
        box().fetch("nope")
    with pytest.raises(NotFound):
        box().trash("nope")


def test_send_files_a_copy_as_sent_and_delivers_it_to_the_owner():
    mb = box()
    ref = mb.send(Outgoing(sender=OWNER, to=OWNER, subject="Digest", body="hello",
                           message_id="<d1@inbox-janitor.invalid>"))
    sent = mb.fetch(ref)
    assert {"SENT", "INBOX"} <= sent.labels and sent.sender == OWNER
    assert mb.sent[0].subject == "Digest"


def test_outgoing_renders_threading_headers():
    out = Outgoing(sender=OWNER, to=OWNER, subject="Re: Digest", body="undo 3",
                   message_id="<r1@example.com>", in_reply_to="<d1@inbox-janitor.invalid>",
                   references=("<d1@inbox-janitor.invalid>",))
    parsed = email.message_from_bytes(out.to_bytes())
    assert parsed["Message-ID"] == "<r1@example.com>"
    assert parsed["In-Reply-To"] == "<d1@inbox-janitor.invalid>"
    assert parsed["References"] == "<d1@inbox-janitor.invalid>"
    assert parsed.get_payload().strip() == "undo 3"


def test_the_json_mailbox_persists_every_change(tmp_path):
    path = tmp_path / "mailbox.json"
    path.write_text(json.dumps({"owner": OWNER, "messages": [{
        "ref": "m1", "from": "Mill Weekly <news@flour.example.com>", "subject": "October news",
        "body": "Rye season.", "received": "2026-10-10T08:15:00Z", "labels": ["INBOX"],
        "headers": {"List-Unsubscribe": "<mailto:unsub@flour.example.com>"},
        "attachments": [{"name": "menu.pdf", "mime": "application/pdf", "size": 1200}],
    }]}), encoding="utf-8")
    mb = JsonFileMailbox(path)
    m = mb.fetch("m1")
    assert (m.sender, m.sender_name, m.header("list-unsubscribe")) == (
        "news@flour.example.com", "Mill Weekly", "<mailto:unsub@flour.example.com>")
    assert m.attachments[0].size == 1200
    mb.modify("m1", add=["Janitor/Newsletters"], remove=["INBOX"])
    assert JsonFileMailbox(path).fetch("m1").labels == {"Janitor/Newsletters"}
