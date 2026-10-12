import pytest
from factories import OWNER, msg, newsletter, promotion

from inbox_janitor.commands import Command, CommandContext, apply, new_text, parse_commands
from inbox_janitor.mailbox.memory import MemoryMailbox
from inbox_janitor.privacy import Fingerprinter
from inbox_janitor.storage import Ledger, State
from inbox_janitor.userrules import UserRules

# -- parsing --------------------------------------------------------------------------------


def one(text: str) -> Command:
    commands, _ = parse_commands(text)
    assert len(commands) == 1, commands
    return commands[0]


@pytest.mark.parametrize(("text", "tickets"), [
    ("undo 3", (3,)), ("Undo #3, #5 and 7", (3, 5, 7)), ("restore 4", (4,)),
    ("- undo #12", (12,)), ("UNDO 3.", (3,)),
])
def test_undo_takes_one_or_more_ticket_numbers(text, tickets):
    assert one(text) == Command("undo", tickets=tickets)


@pytest.mark.parametrize("text", ["undo last", "undo last run", "Undo the last run"])
def test_undo_last(text):
    assert one(text) == Command("undo_last")


def test_keep():
    assert one("keep 12") == Command("keep", tickets=(12,))


@pytest.mark.parametrize(("text", "pattern", "category"), [
    ("never trash newsletters from news@flour.example.com", "news@flour.example.com",
     "newsletter"),
    ("Never trash promotions from @ovens.example.com", "@ovens.example.com", "promotion"),
    ("never trash from flour.example.com", "flour.example.com", None),
    ("never trash anything from flour.example.com", "flour.example.com", None),
    ("never trash mail from billing@flour.example.com", "billing@flour.example.com", None),
])
def test_never_trash(text, pattern, category):
    assert one(text) == Command("never_trash", pattern=pattern, category=category)


@pytest.mark.parametrize("text", ["never touch accountant@books.example.com",
                                  "leave accountant@books.example.com alone"])
def test_never_touch(text):
    assert one(text) == Command("never_touch", pattern="accountant@books.example.com")


@pytest.mark.parametrize("text", ["trust billing@flour.example.com",
                                  "not spam: billing@flour.example.com"])
def test_trust(text):
    assert one(text) == Command("trust", pattern="billing@flour.example.com")


def test_pause_and_resume():
    assert one("pause") == Command("pause")
    assert one("Resume.") == Command("resume")


@pytest.mark.parametrize("text", [
    "stage trash", "set stage to trash", "max-stage trash", "min_confidence 0.5",
    "always trash newsletters from news@flour.example.com", "trash 5", "delete 5",
    "never trash newsletters from everyone", "trust", "undo",
])
def test_nothing_can_raise_autonomy_or_widen_what_gets_destroyed(text):
    commands, unknown = parse_commands(text)
    assert commands == [] and unknown == [text]


def test_quoted_text_below_a_reply_header_is_never_read():
    body = ("undo 3\n\nOn Mon, Oct 12, 2026 at 9:00 AM Owner <owner@harborlane.example.com> "
            "wrote:\n> #7 promotion: never touch @phish.example.com\n> undo 9\n")
    assert parse_commands(body)[0] == [Command("undo", tickets=(3,))]


@pytest.mark.parametrize("marker", ["-----Original Message-----",
                                    "From: Owner <owner@harborlane.example.com>\nSent: Monday",
                                    "________________________________"])
def test_other_quote_styles_end_the_new_text(marker):
    body = f"keep 4\n\n{marker}\nnever touch @phish.example.com\n"
    assert parse_commands(body)[0] == [Command("keep", tickets=(4,))]


def test_a_digest_line_quoted_without_markers_never_parses_as_a_command():
    """Digest lines start with a ticket number, so even a client that quotes without '>'
    can't turn a stranger's subject into a command."""
    body = "keep 2\n#7  trash  promotion  never touch @phish.example.com\n"
    assert parse_commands(body)[0] == [Command("keep", tickets=(2,))]


def test_new_text_keeps_a_wrapped_on_wrote_header_out():
    body = ("undo 1\nOn Mon, Oct 12, 2026 at 9:00 AM Owner\n"
            "<owner@harborlane.example.com> wrote:\n> undo 2")
    assert new_text(body).strip() == "undo 1"


def test_only_the_first_lines_are_read():
    body = "\n".join(["thanks!"] * 30 + ["undo 3"])
    assert parse_commands(body)[0] == []


# -- applying -------------------------------------------------------------------------------

@pytest.fixture
def ctx(tmp_path):
    mailbox = MemoryMailbox(OWNER)
    return CommandContext(mailbox=mailbox,
                          ledger=Ledger(tmp_path / "ledger.jsonl", now=lambda: 1_760_000_000.0),
                          state=State(tmp_path / "state.json"),
                          rules=UserRules(tmp_path / "rules.json", Fingerprinter(b"k" * 32)))


def acted(ctx, m, ticket, applied, run="r1", outcome="done"):
    ctx.mailbox.add(m)
    ctx.ledger.append({"kind": "decision", "ref": m.ref, "ticket": ticket, "run": run,
                       "outcome": outcome, "applied": applied})


def test_undo_puts_an_archived_message_back(ctx):
    m = newsletter(labels=("UNREAD", "Janitor/Newsletters"))
    acted(ctx, m, 3, [["label", "Janitor/Newsletters"], ["unlabel", "INBOX"]])
    assert apply(Command("undo", tickets=(3,)), ctx) == ["undo #3: put back"]
    assert ctx.mailbox.fetch(m.ref).labels == {"UNREAD", "INBOX"}
    assert ctx.ledger.undone(m.ref) and ctx.ledger.is_final(m.ref)
    assert apply(Command("undo", tickets=(3,)), ctx) == ["undo #3: already undone"]


def test_undo_takes_a_message_out_of_the_trash(ctx):
    m = promotion(labels=("TRASH",))
    acted(ctx, m, 4, [["trash"]])
    apply(Command("undo", tickets=(4,)), ctx)
    assert ctx.mailbox.fetch(m.ref).labels == {"INBOX"}


def test_undo_reverses_a_rescue_too_so_the_message_ends_where_it_started(ctx):
    m = newsletter(labels=("Janitor/Newsletters",))
    acted(ctx, m, 5, [["unlabel", "SPAM"], ["label", "INBOX"]])
    acted(ctx, m, 5, [["label", "Janitor/Newsletters"], ["unlabel", "INBOX"]], run="r1")
    apply(Command("undo", tickets=(5,)), ctx)
    assert ctx.mailbox.fetch(m.ref).labels == {"SPAM"}


def test_undo_of_a_suggestion_means_leave_it_alone(ctx):
    m = promotion()
    acted(ctx, m, 6, [], outcome="suggested")
    lines = apply(Command("undo", tickets=(6,)), ctx)
    assert "left alone" in lines[0]
    assert ctx.mailbox.writes == [] and ctx.ledger.is_final(m.ref)


def test_undo_of_an_unknown_ticket(ctx):
    assert apply(Command("undo", tickets=(99,)), ctx) == ["undo #99: no such number"]


def test_undo_of_a_message_that_is_gone_for_good(ctx):
    ctx.ledger.append({"kind": "decision", "ref": "purged", "ticket": 8, "run": "r1",
                       "outcome": "done", "applied": [["trash"]]})
    assert "gone" in apply(Command("undo", tickets=(8,)), ctx)[0]
    assert ctx.ledger.is_final("purged")


def test_undo_last_only_touches_the_last_run_that_changed_anything(ctx):
    older, newer = newsletter(labels=("Janitor/Newsletters",)), promotion(labels=("TRASH",))
    acted(ctx, older, 1, [["label", "Janitor/Newsletters"], ["unlabel", "INBOX"]], run="r1")
    acted(ctx, newer, 2, [["trash"]], run="r2")
    ctx.state["last_acting_run"] = "r2"
    assert apply(Command("undo_last"), ctx) == ["undo #2: put back"]
    assert "INBOX" not in ctx.mailbox.fetch(older.ref).labels


def test_keep_settles_a_ticket(ctx):
    m = msg()
    acted(ctx, m, 7, [], outcome="review")
    assert apply(Command("keep", tickets=(7,)), ctx) == ["keep #7: it will be left alone"]
    assert ctx.ledger.is_final(m.ref)


def test_rules_are_saved_without_echoing_the_address(ctx):
    lines = apply(Command("never_trash", pattern="news@flour.example.com",
                          category="newsletter"), ctx)
    assert ctx.rules.never_trash("news@flour.example.com", "newsletter")
    assert "flour" not in lines[0] and "newsletter" in lines[0]
    again = apply(Command("never_trash", pattern="news@flour.example.com",
                          category="newsletter"), ctx)
    assert "already" in again[0]


def test_pause_and_resume_flip_the_switch(ctx):
    apply(Command("pause"), ctx)
    assert ctx.state["paused"] is True
    apply(Command("resume"), ctx)
    assert ctx.state["paused"] is False
