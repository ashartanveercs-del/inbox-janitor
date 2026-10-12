import hashlib

import pytest
from factories import OWNER, invoice, msg, newsletter, promotion
from fakes import SERVER, FlakyMailbox, ScriptedClassifier, World, auth_ok

from inbox_janitor.janitor import WrongAccount
from inbox_janitor.models import Classification, Message
from inbox_janitor.stages import Stage
from inbox_janitor.storage import Ledger


class Crash(BaseException):
    """What a killed process looks like from inside: not an Exception, nothing catches it."""


class CrashingMailbox(FlakyMailbox):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.crash_on: set[str] = set()      # reading these kills the process
        self.unreadable: set[str] = set()    # reading these raises what a parser bug raises

    def fetch(self, ref: str) -> Message:
        if ref in self.crash_on:
            raise Crash(ref)
        if ref in self.unreadable:
            self.fetched.append(ref)
            raise UnicodeError("undefined encoding")
        return super().fetch(ref)


def crashing_world(tmp_path, *messages, stage=Stage.FILE, **kw) -> World:
    w = World(tmp_path, *messages, stage=stage, **kw)
    w.mailbox = CrashingMailbox(OWNER, messages, serv_id=SERVER, now=w.clock.now)
    return w


def stored_labels(w: World, m: Message) -> set[str]:
    """The labels as the mailbox holds them, without a fetch that may be rigged to fail."""
    return set(w.mailbox.messages[m.ref].labels)


def test_one_bad_message_never_stops_the_rest(tmp_path):
    a, b, c = newsletter(), promotion(), invoice()
    w = World(tmp_path, a, b, c, stage=Stage.FILE)
    w.mailbox.fail_fetch = {b.ref}
    report = w.run()
    assert "INBOX" not in w.labels(a) and "Janitor/Invoices" in w.labels(c)
    assert "mailbox" in report.problems


def test_a_message_that_keeps_failing_is_reported_after_three_tries(tmp_path):
    bad, good = promotion(), newsletter()
    w = World(tmp_path, bad, good, stage=Stage.FILE)
    w.mailbox.fail_fetch = {bad.ref}
    for _ in range(3):
        report = w.run()
        w.clock.advance(days=1)
    assert [i.outcome for i in report.items if i.ref == bad.ref] == ["failed"]
    assert "Couldn't be processed" in w.digests()[-1].body
    tries = w.mailbox.fetched.count(bad.ref)
    w.run()
    assert w.mailbox.fetched.count(bad.ref) == tries     # given up on: not fetched again


def test_three_failures_in_a_row_halt_the_run(tmp_path):
    messages = [newsletter(subject=f"Issue {n}", body=f"Issue {n} of the mill newsletter.")
                for n in range(4)]
    w = World(tmp_path, *messages, stage=Stage.FILE)
    oldest_first = sorted(messages, key=lambda m: m.received)
    w.mailbox.fail_fetch = {m.ref for m in oldest_first[:3]}
    report = w.run()
    assert report.halted and "halted" in report.problems
    assert "INBOX" in w.labels(oldest_first[3])          # left for the next run


def test_a_message_that_kills_the_process_cannot_block_every_later_run(tmp_path):
    poison, fine = msg(subject="Huge attachment"), newsletter()
    w = World(tmp_path, poison, fine, stage=Stage.FILE)
    w.mailbox = CrashingMailbox("owner@harborlane.example.com", [poison, fine],
                                serv_id="mx.example.com", now=w.clock.now)
    w.mailbox.crash_on = {poison.ref}
    for _ in range(3):
        with pytest.raises(Crash):
            w.run()
        w.clock.advance(days=1)
    report = w.run()                                     # the fourth run skips it
    assert {i.ref: i.outcome for i in report.items}[poison.ref] == "failed"
    assert "INBOX" not in w.labels(fine)


def test_one_spam_message_that_fails_to_parse_does_not_stop_triage(tmp_path):
    """Spam is where a stranger's mail lands by default, and it is read before the inbox."""
    junk, fine = promotion(labels=("SPAM",)), newsletter()
    w = crashing_world(tmp_path, junk, fine)
    w.mailbox.unreadable = {junk.ref}
    report = w.run()
    assert "INBOX" not in w.labels(fine) and stored_labels(w, junk) == {"SPAM"}
    assert "error" in report.problems


def test_a_spam_message_that_keeps_failing_is_reported_after_three_tries(tmp_path):
    junk, fine = promotion(labels=("SPAM",)), newsletter()
    w = crashing_world(tmp_path, junk, fine)
    w.mailbox.unreadable = {junk.ref}
    for _ in range(3):
        report = w.run()
        w.clock.advance(days=1)
    assert [i.outcome for i in report.items if i.ref == junk.ref] == ["failed"]
    tries = w.mailbox.fetched.count(junk.ref)
    w.run()
    assert w.mailbox.fetched.count(junk.ref) == tries     # given up on: not read again
    assert stored_labels(w, junk) == {"SPAM"}


def test_a_spam_message_that_kills_the_process_cannot_block_every_later_run(tmp_path):
    poison, fine = promotion(labels=("SPAM",)), newsletter()
    w = crashing_world(tmp_path, poison, fine)
    w.mailbox.crash_on = {poison.ref}
    for _ in range(3):
        with pytest.raises(Crash):
            w.run()
        w.clock.advance(days=1)
    report = w.run()                                     # the fourth run skips it
    assert {i.ref: i.outcome for i in report.items}[poison.ref] == "failed"
    assert "INBOX" not in w.labels(fine) and stored_labels(w, poison) == {"SPAM"}


def test_three_spam_failures_in_a_row_stop_the_spam_check_but_not_triage(tmp_path):
    junk = [promotion(labels=("SPAM",), subject=f"Prize {n}", body=f"Prize number {n} inside.")
            for n in range(4)]
    fine = newsletter()
    w = crashing_world(tmp_path, *junk, fine)
    w.mailbox.fail_fetch = {m.ref for m in junk}           # the mailbox is having a bad day
    report = w.run()
    assert sum(w.mailbox.fetched.count(m.ref) for m in junk) == 3
    assert "spam_halted" in report.problems and not report.halted
    assert "INBOX" not in w.labels(fine)


def test_the_wrong_account_is_refused_before_anything_happens(tmp_path):
    w = World(tmp_path, promotion(), stage=Stage.TRASH)
    w.mailbox.address_override = "someone-else@example.com"
    with pytest.raises(WrongAccount):
        w.run()
    assert w.mailbox.writes == []
    assert not (w.state_dir / "ledger.jsonl").exists()


def test_an_unreachable_classifier_means_wait_then_review(tmp_path):
    n = newsletter()
    down = ScriptedClassifier(default=Classification.unavailable("llm"))
    w = World(tmp_path, n, stage=Stage.FILE, classifier=down)
    first = w.run()
    assert [i.outcome for i in first.items] == ["wait"]
    assert "classifier_unavailable" in first.problems and w.changes() == []
    w.clock.advance(days=4)
    later = w.run()
    assert [(i.outcome, i.reason) for i in later.items] == [("review",
                                                            "classifier_unavailable")]


def test_undoing_a_rescue_returns_it_to_spam_for_good(tmp_path):
    caught = invoice(labels=("SPAM",),
                     headers={"Authentication-Results": auth_ok("flour.example.com")})
    w = World(tmp_path, caught, stage=Stage.FILE,
              config={"trusted_senders": ["@flour.example.com"]})
    report = w.run()
    t = report.items[0].ticket
    w.reply(f"undo {t}")
    w.run()
    assert w.labels(caught) == {"SPAM"}
    w.clock.advance(days=1)
    w.run()
    assert w.labels(caught) == {"SPAM"}


def test_a_damaged_state_file_is_reported_and_tickets_are_never_reused(tmp_path):
    w = World(tmp_path, newsletter(), invoice(), stage=Stage.FILE)
    first = w.run()
    (w.state_dir / "state.json").write_text("{torn", encoding="utf-8")
    late = promotion(received=w.clock.now())
    w.mailbox.add(late)
    report = w.run()
    assert {i.ref: i.ticket for i in report.items}[late.ref] > max(i.ticket for i in first.items)
    assert "state_reset" in report.problems


def test_a_digest_that_cannot_be_sent_keeps_its_corrections_for_next_time(tmp_path):
    n = newsletter()
    w = World(tmp_path, n, stage=Stage.FILE)
    t = next(i.ticket for i in w.run().items)
    w.reply(f"undo {t}")
    w.mailbox.fail_send = True
    report = w.run()
    assert not report.digest_sent
    w.mailbox.fail_send = False
    w.clock.advance(hours=1)
    again = w.run()
    assert again.digest_sent and f"undo #{t}: put back" in w.digests()[-1].body
    assert "digest" in again.problems


def test_nothing_on_disk_holds_a_subject_an_address_or_a_body(tmp_path):
    """The privacy canary: run every feature, then search every file the janitor wrote."""
    caught = invoice(labels=("SPAM",), sender_name="Rye & Co Billing",
                     headers={"Authentication-Results": auth_ok("flour.example.com")})
    messages = [newsletter(sender_name="Mill Weekly"), promotion(sender_name="Oven Outlet"),
                msg(sender="ana.lopez@corner-cafe.example.com", subject="Catering for 40 on Friday",
                    body="Can you do two dozen focaccia and a cake?"), caught]
    w = World(tmp_path, *messages, stage=Stage.TRIAL,
              config={"trusted_senders": ["@flour.example.com"]})
    w.run()
    w.run(Stage.FILE)
    w.reply("never trash newsletters from news@flour.example.com\ntrust @bank.example.com\n"
            "never touch ana.lopez@corner-cafe.example.com\nundo 1")
    w.clock.advance(days=1)
    w.run(Stage.TRASH)

    on_disk = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                        for p in w.state_dir.rglob("*") if p.is_file() and p.suffix != ".key")
    secrets = set()
    for m in messages:
        secrets |= {m.subject, m.sender, m.sender.split("@")[1], m.body[:25], m.sender_name}
        secrets.add(hashlib.sha256(m.sender.encode()).hexdigest()[:16])
    secrets |= {"bank.example.com", "corner-cafe", "focaccia", "Catering"}
    leaks = sorted(s for s in secrets if s and s.lower() in on_disk.lower())
    assert leaks == []
    assert on_disk.count("\n") > 10                  # the ledger really was written


def test_the_run_log_holds_counts_only(tmp_path):
    w = World(tmp_path, newsletter(), promotion(), stage=Stage.FILE)
    report = w.run()
    line = (w.state_dir / "run.log").read_text(encoding="utf-8").strip()
    assert report.run_id in line and "stage=file" in line and "done=1" in line
    assert "@" not in line and "example" not in line


def test_the_ledger_records_each_decision_with_its_confidence(tmp_path):
    n = newsletter()
    w = World(tmp_path, n, stage=Stage.FILE)
    w.run()
    row = Ledger(w.state_dir / "ledger.jsonl").decision(n.ref)
    assert (row["category"], row["confidence"], row["outcome"], row["stage"]) == (
        "newsletter", 0.96, "done", "file")
    assert set(row["fp"]) == {"sender", "msgid", "content"}
