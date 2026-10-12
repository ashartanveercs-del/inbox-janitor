"""Corrections by reply: the owner answers a digest with one command per line.

    undo 3 / undo #3, #5 / restore 3     put those messages back exactly as they were
    undo last                            put back everything the last acting run changed
    keep 7                               leave that message alone from now on
    never trash [newsletters] from X     X = name@example.com, @example.com or example.com
    never touch X / leave X alone
    trust X / not spam: X                rescue X's authenticated mail from Spam
    pause / resume

The grammar is deliberately one-directional: every command makes the janitor more careful,
reverses something, or (trust) protects legitimate mail. There is no command that raises the
stage, lowers the floor or adds a way to destroy mail - those live in the operator's config.

Only the reply's own text is read (never the quoted digest), and digest lines start with a
ticket number, which no command does - so a stranger's subject quoted back by the owner's
mail client can't become a command.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .addresses import is_sender_pattern
from .classify import CATEGORIES
from .mailbox import Mailbox, NotFound
from .storage import Ledger, State
from .userrules import UserRules

MAX_LINES = 20

_QUOTE_START = re.compile(
    r"(?im)^(?:>"                                                   # quoted line
    r"|on\b[^\n]{0,200}(?:\n[^\n]{0,200})?\bwrote:[ \t]*$"          # On <date>, <who> wrote:
    r"|-{2,}\s*original message\s*-{2,}"                            # Outlook
    r"|_{10,}"                                                      # Outlook separator
    r"|from:[^\n]*\n\s*sent:)"                                      # Outlook header block
)
_TICKETS = r"#?\d{1,6}(?:\s*(?:,|and|&)?\s*#?\d{1,6})*"
_PATTERNS = (
    ("undo_last", re.compile(r"^undo\s+(?:the\s+)?last(?:\s+run)?$")),
    ("undo", re.compile(rf"^(?:undo|restore)\s+(?P<tickets>{_TICKETS})$")),
    ("keep", re.compile(rf"^keep\s+(?P<tickets>{_TICKETS})$")),
    ("never_trash", re.compile(r"^never\s+trash\s+(?:(?P<what>[a-z]+)\s+)?from\s+(?P<who>\S+)$")),
    ("never_touch", re.compile(r"^never\s+touch\s+(?P<who>\S+)$")),
    ("never_touch", re.compile(r"^leave\s+(?P<who>\S+)\s+alone$")),
    ("trust", re.compile(r"^(?:trust|not\s+spam:?)\s+(?P<who>\S+)$")),
    ("pause", re.compile(r"^pause$")),
    ("resume", re.compile(r"^resume$")),
)
_EVERYTHING = {"anything", "everything", "all", "mail", "email", "emails", "messages"}
_PLURAL = {"support": "support mail", "personal": "personal mail", "other": "other mail"}


@dataclass(frozen=True)
class Command:
    verb: str
    tickets: tuple[int, ...] = ()
    pattern: str | None = None
    category: str | None = None


def new_text(body: str) -> str:
    """The reply's own words: everything above the first sign of quoted text."""
    match = _QUOTE_START.search(body or "")
    return (body or "")[: match.start()] if match else (body or "")


def _category(word: str | None) -> tuple[bool, str | None]:
    if word is None or word in _EVERYTHING:
        return True, None
    for c in CATEGORIES:
        if word in (c, c + "s"):
            return True, c
    return False, None


def _parse_line(line: str) -> Command | None:
    text = line.strip().lstrip("-*•").strip().rstrip(".!").strip().lower()
    for verb, pattern in _PATTERNS:
        m = pattern.match(text)
        if not m:
            continue
        groups = m.groupdict()
        if "tickets" in groups:
            return Command(verb, tickets=tuple(int(n) for n in re.findall(r"\d+",
                                                                         groups["tickets"])))
        if "who" in groups:
            if not is_sender_pattern(groups["who"]):
                return None
            ok, category = _category(groups.get("what"))
            return Command(verb, pattern=groups["who"], category=category) if ok else None
        return Command(verb)
    return None


def parse_commands(body: str) -> tuple[list[Command], list[str]]:
    """(commands, lines that weren't understood) from the first MAX_LINES lines."""
    commands, unknown = [], []
    for line in new_text(body).splitlines()[:MAX_LINES]:
        if not line.strip():
            continue
        cmd = _parse_line(line)
        if cmd is None:
            unknown.append(line.strip())
        else:
            commands.append(cmd)
    return commands, unknown


# -- applying ---------------------------------------------------------------------------------

@dataclass
class CommandContext:
    mailbox: Mailbox
    ledger: Ledger
    state: State
    rules: UserRules


def reversal(history: list[list[str]]) -> tuple[bool, set[str], set[str]]:
    """(untrash?, labels to add, labels to remove) that restore the state before the first
    change. A label's original presence is the opposite of the first change made to it;
    trashing counts as removing INBOX (only inbox mail is ever trashed)."""
    first: dict[str, str] = {}
    untrash = False
    for op in history:
        if op[0] == "trash":
            untrash = True
            first.setdefault("INBOX", "unlabel")
        elif op[0] in ("label", "unlabel") and len(op) == 2:
            first.setdefault(op[1], op[0])
    add = {label for label, op in first.items() if op == "unlabel"}
    remove = {label for label, op in first.items() if op == "label"}
    return untrash, add, remove


def _undo(ctx: CommandContext, ticket: int) -> str:
    ref = ctx.ledger.ref_of(ticket)
    if ref is None:
        return f"undo #{ticket}: no such number"
    if ctx.ledger.undone(ref):
        return f"undo #{ticket}: already undone"
    history = ctx.ledger.history(ref)
    if not history:
        ctx.ledger.append({"kind": "keep", "ticket": ticket})
        return f"undo #{ticket}: nothing had been done to it; it will be left alone"
    untrash, add, remove = reversal(history)
    try:
        if untrash:
            ctx.mailbox.untrash(ref)
        if add or remove:
            ctx.mailbox.modify(ref, add=sorted(add), remove=sorted(remove))
    except NotFound:
        ctx.ledger.append({"kind": "undo", "ticket": ticket, "ok": False})
        return f"undo #{ticket}: the message is gone for good; nothing to put back"
    ctx.ledger.append({"kind": "undo", "ticket": ticket, "ok": True})
    return f"undo #{ticket}: put back"


def _keep(ctx: CommandContext, ticket: int) -> str:
    if ctx.ledger.ref_of(ticket) is None:
        return f"keep #{ticket}: no such number"
    ctx.ledger.append({"kind": "keep", "ticket": ticket})
    return f"keep #{ticket}: it will be left alone"


def _describe_rule(cmd: Command) -> str:
    if cmd.verb == "never_trash":
        what = (_PLURAL.get(cmd.category, f"{cmd.category}s") if cmd.category else "anything")
        return f"never trash {what} from that sender"
    if cmd.verb == "never_touch":
        return "never touch that sender's mail"
    return "rescue that sender's authenticated mail from Spam"


def apply(cmd: Command, ctx: CommandContext) -> list[str]:
    """Result lines for the next digest. Idempotent, so a reply that failed halfway can be
    applied again from the top. Mailbox errors other than NotFound propagate."""
    if cmd.verb == "undo":
        return [_undo(ctx, t) for t in cmd.tickets]
    if cmd.verb == "undo_last":
        run = ctx.state.get("last_acting_run")
        tickets = sorted({row["ticket"] for row in ctx.ledger.rows
                          if row.get("kind") == "decision" and row.get("run") == run
                          and run and row.get("applied")
                          and not ctx.ledger.undone(row["ref"])})
        return [_undo(ctx, t) for t in tickets] or ["undo last run: nothing to undo"]
    if cmd.verb == "keep":
        return [_keep(ctx, t) for t in cmd.tickets]
    if cmd.verb in ("never_trash", "never_touch", "trust"):
        added = ctx.rules.add(cmd.verb, cmd.pattern or "", cmd.category)
        return [("rule saved: " if added else "rule already in place: ") + _describe_rule(cmd)]
    if cmd.verb in ("pause", "resume"):
        ctx.state["paused"] = cmd.verb == "pause"
        ctx.state.save()
        return ["paused: nothing will be filed, labelled or trashed until you reply \"resume\""
                if cmd.verb == "pause" else "resumed"]
    raise ValueError(f"unknown command {cmd.verb!r}")
