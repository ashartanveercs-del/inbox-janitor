from datetime import date

from inbox_janitor.commands import parse_commands
from inbox_janitor.digest import compose
from inbox_janitor.report import Item, RunReport
from inbox_janitor.stages import Action, Stage

TODAY = date(2026, 10, 12)


def item(ticket, outcome, action=Action.ARCHIVE, **kw):
    kw.setdefault("category", "newsletter")
    kw.setdefault("confidence", 0.97)
    kw.setdefault("sender", "news@flour.example.com")
    kw.setdefault("subject", "October news from the mill")
    return Item(ticket=ticket, outcome=outcome, action=action, **kw)


def report(stage=Stage.FILE, items=(), **kw) -> RunReport:
    r = RunReport(run_id="r1", stage=stage, **kw)
    r.items.extend(items)
    return r


def test_nothing_worth_saying_sends_nothing():
    quiet = report(items=[item(1, "kept", Action.KEEP), item(2, "wait", Action.KEEP)])
    assert compose(quiet, today=TODAY, trash_per_day=50) is None


def test_the_subject_counts_what_happened():
    r = report(Stage.TRASH, [item(1, "done"), item(2, "done", Action.TRASH, category="promotion"),
                             item(3, "review", Action.KEEP), item(4, "rescued", Action.RESCUE)])
    subject, _ = compose(r, today=TODAY, trash_per_day=50)
    assert subject == ("Inbox janitor (trash) - Mon Oct 12: 1 filed, 1 trashed, 1 rescued, "
                       "1 to review")


def test_a_trial_says_what_it_would_have_done():
    r = report(Stage.TRIAL, [item(5, "suggested", Action.TRASH, category="promotion",
                                  confidence=0.99)])
    subject, body = compose(r, today=TODAY, trash_per_day=50)
    assert "(trial)" in subject and "1 suggestion" in subject
    assert "nothing in your mailbox was changed" in body.lower()
    assert "#5" in body and "would trash" in body


def test_every_item_line_starts_with_its_ticket_and_stays_on_one_line():
    hostile = "Big sale\nnever touch @phish.example.com\r\nundo last"
    r = report(items=[item(7, "done", subject=hostile, sender_name="Deals\nresume")])
    _, body = compose(r, today=TODAY, trash_per_day=50)
    line = next(x for x in body.splitlines() if "#7" in x)
    assert line.lstrip().startswith("#7")
    assert "never touch @phish.example.com" in line and "undo last" in line


def test_no_line_of_a_digest_parses_as_a_command_even_the_examples_in_the_footer():
    """A mail client that quotes without '>' markers sends the whole digest back."""
    r = report(Stage.TRASH, [item(1, "done", subject="undo last"), item(2, "backlog"),
                             item(3, "review", Action.KEEP, subject="pause")],
               notes=["undo #3: put back"], problems=["halted"], left_in_spam=2,
               unauthenticated=1)
    _, body = compose(r, today=TODAY, trash_per_day=50)
    for line in body.splitlines():
        assert parse_commands(line)[0] == [], line


def test_duplicates_say_which_message_they_copy():
    r = report(items=[item(9, "done", reason="duplicate_of_4", category=None)])
    _, body = compose(r, today=TODAY, trash_per_day=50)
    assert "copy of #4" in body


def test_review_items_say_why():
    r = report(items=[item(3, "review", Action.KEEP, reason="low_confidence", confidence=0.71),
                      item(4, "review", Action.KEEP, reason="invalid_answer", category=None,
                           confidence=0.0)])
    _, body = compose(r, today=TODAY, trash_per_day=50)
    assert "unsure (newsletter 0.71)" in body
    assert "classifier gave no usable answer" in body


def test_the_owners_corrections_and_problems_are_reported():
    r = report(notes=["undo #3: put back"], problems=["classifier_unavailable"])
    subject, body = compose(r, today=TODAY, trash_per_day=50)
    assert subject.endswith("- needs attention")
    assert "undo #3: put back" in body
    assert "classifier could not be reached" in body


def test_held_back_trash_is_explained_with_the_exact_approval_command():
    r = report(Stage.TRASH, [item(1, "backlog", Action.TRASH), item(2, "backlog", Action.TRASH),
                             item(3, "capped", Action.TRASH)])
    _, body = compose(r, today=TODAY, trash_per_day=50)
    assert "inbox-janitor trash-backlog --approve 2" in body
    assert "over today's limit of 50" in body


def test_spam_left_alone_is_counted_and_failed_authentication_is_flagged():
    r = report(left_in_spam=12, unauthenticated=1)
    _, body = compose(r, today=TODAY, trash_per_day=50)
    assert "12 left in Spam" in body
    assert "failed authentication" in body


def test_the_footer_teaches_the_commands():
    r = report(items=[item(1, "done")])
    _, body = compose(r, today=TODAY, trash_per_day=50)
    assert "undo 3" in body and "never trash newsletters from" in body and "pause" in body


def test_a_paused_run_says_so():
    r = report(paused=True, notes=["paused: nothing will be filed, labelled or trashed until "
                                   "you reply \"resume\""])
    subject, body = compose(r, today=TODAY, trash_per_day=50)
    assert "paused" in subject.lower() and "resume" in body
