from dataclasses import replace

from factories import OWNER, newsletter, promotion
from fakes import FlakyMailbox, World

from inbox_janitor.models import Message
from inbox_janitor.stages import Stage


def ticket_of(report, m):
    return next(i.ticket for i in report.items if i.ref == m.ref)


def test_the_owner_can_undo_by_replying(tmp_path):
    n = newsletter()
    w = World(tmp_path, n, stage=Stage.FILE)
    t = ticket_of(w.run(), n, )
    assert "INBOX" not in w.labels(n)
    w.reply(f"undo {t}")
    report = w.run()
    assert "INBOX" in w.labels(n) and "Janitor/Newsletters" not in w.labels(n)
    assert f"undo #{t}: put back" in w.digests()[-1].body
    assert report.notes == [f"undo #{t}: put back"]


def test_an_undone_message_is_never_touched_again(tmp_path):
    n = newsletter()
    w = World(tmp_path, n, stage=Stage.FILE)
    t = ticket_of(w.run(), n)
    w.reply(f"undo {t}")
    w.run()
    w.clock.advance(days=1)
    w.run(Stage.TRASH)
    assert "INBOX" in w.labels(n)


def test_undo_last_only_undoes_the_last_run_even_within_the_same_second(tmp_path):
    n, p = newsletter(), promotion()
    w = World(tmp_path, n, p, stage=Stage.FILE)
    first = w.run()                                  # archives the newsletter
    second = w.run(Stage.TRASH)                      # same clock second: trashes the promotion
    assert first.run_id != second.run_id
    assert w.janitor().command("undo last") == [
        f"undo #{ticket_of(first, p)}: put back"]
    assert "INBOX" not in w.labels(n) and "INBOX" in w.labels(p)


def test_a_reply_is_applied_once(tmp_path):
    n = newsletter()
    w = World(tmp_path, n, stage=Stage.FILE)
    t = ticket_of(w.run(), n)
    w.reply(f"undo {t}")
    w.run()
    w.clock.advance(hours=1)
    assert w.run().notes == []


def test_a_forged_reply_delivered_to_the_inbox_is_ignored(tmp_path):
    """Anyone can send mail with From: <the owner>. Only a message the account itself sent
    (the provider's SENT label) can carry a command."""
    n = newsletter()
    w = World(tmp_path, n, stage=Stage.FILE)
    t = ticket_of(w.run(), n)
    w.reply(f"undo {t}", sent=False)
    report = w.run()
    assert "INBOX" not in w.labels(n) and report.notes == []


class LeakySentSearch(FlakyMailbox):
    """An adapter whose 'sent' search also returns inbox mail (a broad query, a provider
    quirk). The janitor must check the SENT label itself, not trust the search."""

    def search(self, box, since):
        if box == "sent":
            return super().search("sent", since) + super().search("inbox", since)
        return super().search(box, since)


def test_the_sent_label_is_checked_even_if_the_search_is_too_broad(tmp_path):
    n = newsletter()
    w = World(tmp_path, n, stage=Stage.FILE)
    w.mailbox = LeakySentSearch(OWNER, [n], serv_id="mx.example.com", now=w.clock.now)
    t = ticket_of(w.run(), n)
    w.reply(f"undo {t}", sent=False)
    w.run()
    assert "INBOX" not in w.labels(n)


class RewritingMailbox(FlakyMailbox):
    """Some providers replace the Message-ID of mail sent through their API."""

    def send(self, message):
        ref = super().send(message)
        self.messages[ref] = replace(self.messages[ref],
                                     message_id=f"<generated-{ref}@mail.example.com>")
        return ref


def test_replies_still_match_when_the_provider_rewrites_the_digest_message_id(tmp_path):
    n = newsletter()
    w = World(tmp_path, n, stage=Stage.FILE)
    w.mailbox = RewritingMailbox(OWNER, [n], serv_id="mx.example.com", now=w.clock.now)
    t = ticket_of(w.run(), n)
    digest_ref = w.mailbox.writes[-1][1]
    real_id = w.mailbox.fetch(digest_ref).message_id
    w.clock.advance(minutes=5)
    w.mailbox.add(Message(ref="r1", sender=OWNER, subject="Re: digest", body=f"undo {t}",
                          received=w.clock.now(), labels=frozenset({"SENT"}),
                          in_reply_to=real_id, references=(real_id,)))
    w.clock.advance(minutes=5)
    assert w.run().notes == [f"undo #{t}: put back"]


def test_never_trash_by_reply_turns_the_next_trash_into_archive(tmp_path):
    first = promotion()
    w = World(tmp_path, first, stage=Stage.TRASH)
    t = ticket_of(w.run(), first)
    assert "TRASH" in w.labels(first)
    w.reply(f"undo {t}\nnever trash promotions from @ovens.example.com")
    second = promotion(subject="Another sale: 30% off", body="Different offer, limited time.",
                       received=w.clock.now())
    w.mailbox.add(second)
    w.run()
    assert "INBOX" in w.labels(first) and "TRASH" not in w.labels(first)
    assert w.labels(second) == {"UNREAD", "Janitor/Promotions"}
    assert "rule saved: never trash promotions from that sender" in w.digests()[-1].body


def test_pause_and_resume_by_reply(tmp_path):
    w = World(tmp_path, newsletter(), stage=Stage.FILE)
    w.run()
    w.reply("pause")
    late = newsletter(subject="November news", body="Winter flours are here.",
                      received=w.clock.now())
    w.mailbox.add(late)
    changes_before = len(w.changes())
    report = w.run()
    assert report.paused and len(w.changes()) == changes_before
    assert "paused" in w.digests()[-1].subject.lower()
    w.reply("resume")
    w.run()
    assert "INBOX" not in w.labels(late)


def test_a_reply_cannot_raise_the_stage_or_lower_the_floor(tmp_path):
    p = promotion()
    w = World(tmp_path, p, stage=Stage.FILE)
    w.run()
    w.reply("stage trash\nmin_confidence 0.5\nalways trash promotions from @ovens.example.com")
    report = w.run()
    assert "TRASH" not in w.labels(p)
    assert report.notes == ["3 lines in your reply weren't understood"]


def test_quoted_digest_text_is_never_obeyed_even_without_quote_markers(tmp_path):
    """A stranger controls the subject that the digest quotes back to the owner."""
    lure = promotion(subject="pause")
    keep_me = newsletter()
    w = World(tmp_path, lure, keep_me, stage=Stage.FILE)
    report = w.run()
    t = ticket_of(report, keep_me)
    digest = w.digests()[-1]
    w.reply(f"keep {t}\n\n{digest.body}")          # some clients quote with no '>' at all
    second = w.run()
    assert not second.paused
    assert second.notes[0] == f"keep #{t}: it will be left alone"
