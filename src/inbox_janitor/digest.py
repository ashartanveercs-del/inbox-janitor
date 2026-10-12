"""The digest: one plain-text email to the owner per run that has something to say.

It is also the one place strangers' text (subjects, display names) is shown back to the
owner - in the same thread the owner answers with commands. So every item line starts with
its ticket number and is flattened to a single line, and no line of the digest (the footer's
examples included) starts with a command word: even a reply that quotes the digest without
'>' markers can't turn it into commands.
"""

from __future__ import annotations

from datetime import date

from .privacy import one_line
from .report import Item, RunReport
from .stages import Action, Stage

REPORTABLE = frozenset({"done", "suggested", "review", "rescued", "capped", "backlog",
                        "failed"})
LIST_LIMIT = 25

PROBLEMS = {
    "classifier_unavailable": "The classifier could not be reached; those messages will be "
                              "tried again next run.",
    "mailbox": "The mailbox didn't answer for some messages; they will be tried again next run.",
    "halted": "The run stopped early after several failures in a row; it carries on next run.",
    "state_reset": "The state file was damaged and was set aside; nothing in the mailbox was "
                   "lost.",
    "rules_reset": "The saved rules file was damaged and was set aside; please send the rules "
                   "again.",
    "replies": "A reply couldn't be applied yet; it will be tried again next run.",
    "digest": "The previous digest couldn't be sent; its corrections are included here.",
    "error": "Something went wrong with some messages; they will be tried again, and listed "
             "here if they keep failing.",
}

STAGE_LINES = {
    Stage.TRIAL: "Stage: trial. Nothing in your mailbox was changed, except mail rescued from "
                 "Spam.",
    Stage.FILE: "Stage: file. Labelling and archiving are on; nothing is trashed.",
    Stage.TRASH: "Stage: trash. Trashing is on, at most {cap} a day; trashed mail stays "
                 "recoverable for about 30 days.",
}

_DONE = {Action.LABEL: "labelled", Action.ARCHIVE: "archived", Action.TRASH: "trashed",
         Action.RESCUE: "back in inbox"}
_REVIEW = {"invalid_answer": "classifier gave no usable answer",
           "classifier_unavailable": "classifier unavailable for days"}
FOOTER = [
    "Reply to this email with commands, one per line. For example:",
    '  "undo 3", "undo last", "keep 5", "pause", "resume",',
    '  "never trash newsletters from news@example.com",',
    '  "never touch name@example.com", "trust billing@example.com".',
    "Commands can only undo, protect or pause; the stage is set by whoever runs the janitor.",
]


def _what(i: Item) -> str:
    if i.reason.startswith("duplicate_of_"):
        return "copy of #" + i.reason.removeprefix("duplicate_of_")
    if i.outcome == "review":
        if i.reason == "low_confidence":
            return f"unsure ({i.category} {i.confidence:.2f})"
        return _REVIEW.get(i.reason, "needs a look")
    if i.outcome == "rescued":
        return "trusted sender"
    if i.outcome == "failed":
        return "kept failing; untouched"
    seen = f"{i.category} {i.confidence:.2f}" if i.category else ""
    return seen + (" (never-trash rule)" if i.reason == "never_trash" else "")


def _line(i: Item, verb: str) -> str:
    who = one_line(f"{i.sender_name} <{i.sender}>" if i.sender_name else i.sender, 50)
    subject = one_line(i.subject, 60)
    text = f"  #{i.ticket:<4} {verb:<15} {_what(i):<26} {who}"
    return text + (f'  "{subject}"' if subject else "")


def _section(title: str, items: list[Item], verb) -> list[str]:
    if not items:
        return []
    items = sorted(items, key=lambda i: i.ticket)
    lines = ["", title]
    lines += [_line(i, verb(i)) for i in items[:LIST_LIMIT]]
    if len(items) > LIST_LIMIT:
        lines.append(f"  ...and {len(items) - LIST_LIMIT} more")
    return lines


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def compose(r: RunReport, *, today: date, trash_per_day: int) -> tuple[str, str] | None:
    """(subject, body), or None when the run has nothing worth an email."""
    by = {o: [i for i in r.items if i.outcome == o] for o in REPORTABLE}
    if not (any(by.values()) or r.notes or r.problems or r.unauthenticated):
        return None
    counts = r.counts()

    pieces = ["paused"] if r.paused else []
    pieces += [f"{counts[k]} {word}" for k, word in (("filed", "filed"), ("trashed", "trashed"),
                                                     ("rescued", "rescued")) if counts[k]]
    if counts["suggested"]:
        pieces.append(_plural(counts["suggested"], "suggestion"))
    if counts["review"]:
        pieces.append(f"{counts['review']} to review")
    if not pieces:
        pieces.append("your corrections" if r.notes else "nothing new")
    subject = (f"Inbox janitor ({r.stage}) - {today:%a %b} {today.day}: " + ", ".join(pieces)
               + (" - needs attention" if r.problems else ""))

    body = ['Paused: nothing was filed, labelled or trashed. Reply "resume" to start again.'
            if r.paused else STAGE_LINES[r.stage].format(cap=trash_per_day)]
    body += _section("Done:", by["done"], lambda i: _DONE[i.action])
    body += _section("Rescued from Spam:", by["rescued"], lambda i: _DONE[Action.RESCUE])
    body += _section("Would do, once the stage allows it:", by["suggested"],
                     lambda i: f"would {i.action.value}")
    body += _section("Needs your review (left untouched):", by["review"], lambda i: "review")
    body += _section("Couldn't be processed (left untouched):", by["failed"],
                     lambda i: "failed")
    body += _section("Held back:", by["backlog"] + by["capped"],
                     lambda i: "held back" if i.outcome == "backlog" else "over limit")
    held = []
    if by["backlog"]:
        n = len(by["backlog"])
        held.append(f"{_plural(n, 'trash candidate')} arrived before trashing was turned on. "
                    f"To clear them, the operator runs: inbox-janitor trash-backlog --approve {n}")
    if by["capped"]:
        held.append(f"{len(by['capped'])} over today's limit of {trash_per_day} trashes; tried "
                    "again tomorrow.")
    body += held
    spam = []
    if r.left_in_spam:
        spam.append(f"{r.left_in_spam} left in Spam (only trusted, authenticated senders are "
                    "rescued).")
    if r.unauthenticated:
        spam.append(f"{_plural(r.unauthenticated, 'message')} claiming to be from a trusted "
                    "sender failed authentication and stayed in Spam.")
    body += ["", *spam] if spam else []
    if r.notes:
        body += ["", "Your corrections:"] + [f"  -> {one_line(n, 120)}" for n in r.notes]
    if r.problems:
        body += ["", "Problems:"] + [f"  {PROBLEMS.get(p, p)}" for p in r.problems]
    body += ["", *FOOTER]
    return subject, "\n".join(body) + "\n"
