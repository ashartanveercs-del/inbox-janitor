"""One scheduled run: replies -> spam rescue -> triage -> digest.

    1. Check the mailbox really is the configured owner's (a token for the wrong account
       stops everything before any change).
    2. Apply the owner's reply commands (undo, keep, rules, pause) - even while paused.
    3. Rescue trusted, authenticated mail from Spam (allowed at every stage).
    4. Triage the inbox, oldest first: exemptions, duplicates, classifier, policy, floor,
       owner rules; then the stage gate, the trash backlog line and the caps.
    5. Send one digest if anything is worth saying.

Every message gets a `start` row before it is touched and a `decision` (or `error`) row after,
so a run killed mid-message is detected next time and a message that keeps failing - or keeps
killing the process - is given up on after three tries instead of blocking every run.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .classify import Classifier
from .commands import CommandContext, apply, parse_commands
from .config import Config
from .dedupe import DuplicateIndex, fingerprints
from .digest import compose
from .mailbox import Mailbox, MailboxError, NotFound, Outgoing
from .models import INBOX, SENT, SPAM, Message, is_system_label
from .policy import Verdict, decide, duplicate_verdict, exemption, gate
from .privacy import Fingerprinter, load_or_create_key
from .report import Item, RunReport
from .rescue import judge
from .stages import Action, Stage, effective_stage, permits
from .storage import DAY, Ledger, State, append_private
from .userrules import UserRules

MAX_TRIES = 3            # a message failing this often is reported and left alone for good
HALT_AFTER = 3           # this many failures in a row is an outage, not one bad message
KEEP_DIGESTS = 60        # digest ids remembered for matching replies
REPLY_OVERLAP = timedelta(hours=1)
DIGEST_DOMAIN = "inbox-janitor.invalid"
SETTLED = frozenset({"done", "kept", "review", "backlog", "failed"})
SPAM_MARK = "reading_spam"   # state key: the spam message being read right now


class WrongAccount(Exception):
    """The mailbox belongs to someone other than the configured owner: touch nothing."""


class BacklogMismatch(Exception):
    """The approved count is not the number of messages waiting: approve what you saw."""


class StageTooLow(Exception):
    """The effective stage doesn't permit this; only the operator's pin can raise it."""


class UnsafeChange(Exception):
    """A change outside what filing may do. Refused whatever asked for it."""


class Janitor:
    def __init__(self, *, mailbox: Mailbox, classifier: Classifier, config: Config,
                 pinned_stage: Stage, state_dir: Path | str,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC),
                 fp: Fingerprinter | None = None) -> None:
        self.mailbox = mailbox
        self.classifier = classifier
        self.config = config
        self.stage = effective_stage(pinned_stage, config.stage)
        self.state_dir = Path(state_dir)
        self.now = now
        self.tz = ZoneInfo(config.timezone)
        self.state = State(self.state_dir / "state.json")
        self.ledger = Ledger(self.state_dir / "ledger.jsonl", now=lambda: self.now().timestamp(),
                             keep_days=config.keep_days)
        self.fp = fp or Fingerprinter(load_or_create_key(self.state_dir / "secret.key"))
        self.rules = UserRules(self.state_dir / "rules.json", self.fp)
        self.owners = frozenset({config.owner})
        self._trashed_this_run = 0
        self._run_id = ""

    # -- the run ------------------------------------------------------------------------------

    def run(self) -> RunReport:
        self._check_account()
        self._run_id = self._new_run_id("r")
        report = RunReport(run_id=self._run_id, stage=self.stage)
        report.notes = list(self.state.get("pending_notes", []))
        for code, on in (("state_reset", self.state.was_reset),
                         ("rules_reset", self.rules.was_reset),
                         ("digest", self.state.pop("digest_failed", False))):
            if on:
                report.problem(code)
        for warning in self.config.warnings:
            report.problem(warning)
        try:
            self._read_replies(report)
            report.paused = bool(self.state.get("paused"))
            if not report.paused:
                self._update_trash_since()
                self._rescue(report)
                self._triage(report)
            self._send_digest(report)
        finally:
            self._finish(report)
        return report

    def _new_run_id(self, kind: str) -> str:
        """Time-ordered and unique: two runs in one second must never share an id, or "undo
        last" would reach into the earlier one."""
        return f"{kind}{self.now().astimezone(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"

    def _check_account(self) -> None:
        if self.mailbox.address().strip().lower() != self.config.owner:
            raise WrongAccount("the mailbox does not belong to the configured owner")

    # -- replies --------------------------------------------------------------------------------

    def _read_replies(self, report: RunReport) -> None:
        digests = set(self.state.get("digests", []))
        if not digests:
            return
        started = self.now()
        checked = datetime.fromtimestamp(float(self.state.get("replies_checked", 0)), UTC)
        since = max(self._since(), checked - REPLY_OVERLAP)
        own = set(self.state.get("digest_refs", []))
        try:
            refs = self.mailbox.search("sent", since)
        except MailboxError:
            report.problem("replies")
            return
        ctx = CommandContext(self.mailbox, self.ledger, self.state, self.rules)
        retry = False
        for ref in reversed(refs):
            key = self.fp("reply", ref)
            if ref in own or self.ledger.seen_reply(key):
                continue
            try:
                m = self.mailbox.fetch(ref)
                # only a message this account itself sent, answering one of our digests
                if SENT not in m.labels or m.sender != self.config.owner \
                        or not ({m.in_reply_to, *m.references} & digests):
                    continue
                commands, unknown = parse_commands(m.body)
                notes: list[str] = []
                for cmd in commands:
                    notes += apply(cmd, ctx)
            except MailboxError:
                report.problem("replies")          # not marked seen: applied again next run
                retry = True
                continue
            if unknown:
                notes.append("1 line in your reply wasn't understood" if len(unknown) == 1
                             else f"{len(unknown)} lines in your reply weren't understood")
            report.notes += notes
            self.ledger.append({"kind": "reply", "fp": key})
            self.state["pending_notes"] = report.notes        # survives a crash before sending
            self.state.save()
        # The window only moves on once every reply in it was read and applied: a reply that
        # failed must still be inside the next run's search, however much later that runs.
        if not retry:
            self.state["replies_checked"] = started.timestamp()

    # -- spam rescue ----------------------------------------------------------------------------

    def _rescue(self, report: RunReport) -> None:
        """Spam gets the same poison accounting as the inbox: a stranger's mail lands here by
        default, and this runs first, so no single message may stop it. Spam left in place
        writes nothing to the ledger (most of it is re-read every run), so the message being
        read is noted in the state file instead: a run that dies reading it blames it."""
        died_on = self.state.pop(SPAM_MARK, None)
        if died_on:
            self.ledger.append({"kind": "error", "ref": died_on, "ticket": self._ticket(died_on),
                                "what": "Killed"})
        try:
            refs = self.mailbox.search("spam", self._since())
        except MailboxError:
            report.problem("mailbox")
            return
        streak = 0
        for ref in reversed(refs):
            # Already handled once (rescued, or triaged in the inbox) and in Spam again: the
            # owner (or an undo) put it there on purpose. Rescuing it again would be a daily
            # tug-of-war with the owner's own "Report spam".
            if self.ledger.is_final(ref) or self.ledger.decision(ref) is not None:
                report.left_in_spam += 1
                continue
            if self.ledger.failures(ref) >= MAX_TRIES:
                self._give_up(ref, report)
                continue
            self.state[SPAM_MARK] = ref
            self.state.save()
            try:
                self._rescue_one(ref, report)
                streak = 0
            except Exception as err:                # one bad message never stops the others
                streak += 1
                self._failed(ref, err, report)
                if streak >= HALT_AFTER:            # an outage: stop asking, triage still runs
                    report.problem("spam_halted")
                    break
        self.state.pop(SPAM_MARK, None)

    def _rescue_one(self, ref: str, report: RunReport) -> None:
        m = self.mailbox.fetch(ref)
        verdict = judge(m, self.config.trusted_senders, self.mailbox.serv_id,
                        also_trusted=self.rules.trusted)
        if not verdict.rescue:
            report.left_in_spam += 1
            report.unauthenticated += verdict.reason == "unauthenticated"
            return
        # RESCUE is permitted at every stage: in trial, doing nothing loses mail
        ticket = self._ticket(ref)
        applied = self._change(m, add=[INBOX], remove=[SPAM])
        self.ledger.append(self._row(m, ticket, Verdict("act", Action.RESCUE, "trusted"),
                                     "rescued", applied))
        self.state["last_acting_run"] = self._run_id
        report.items.append(self._item(m, ticket, "rescued",
                                       Verdict("act", Action.RESCUE, "trusted")))

    # -- triage ---------------------------------------------------------------------------------

    def _triage(self, report: RunReport) -> None:
        try:
            refs = self.mailbox.search("inbox", self._since())
        except MailboxError:
            report.problem("mailbox")
            return
        dups = DuplicateIndex.from_ledger(self.ledger)
        own = set(self.state.get("digest_refs", []))
        streak = 0
        for ref in reversed(refs):                  # oldest first: the first copy is original
            if ref in own or not self._due(ref):
                continue
            if self.ledger.cut_off(ref):            # a run died while working on it
                self.ledger.append({"kind": "error", "ref": ref, "ticket": self._ticket(ref),
                                    "what": "Killed"})
            if self.ledger.failures(ref) >= MAX_TRIES:
                self._give_up(ref, report)
                continue
            if report.halted:
                continue
            try:
                self._process(ref, dups, report)
                streak = 0
            except Exception as err:                # one bad message never stops the others
                streak += 1
                self._failed(ref, err, report)
                if streak >= HALT_AFTER:
                    report.halted = True
                    report.problem("halted")

    def _due(self, ref: str) -> bool:
        if self.ledger.is_final(ref):
            return False
        row = self.ledger.decision(ref)
        if row is None:
            return True
        outcome = row.get("outcome")
        if outcome in SETTLED:
            return False
        if outcome == "suggested":                  # only once the stage allows it
            return permits(self.stage, Action(row.get("wanted", "keep")))
        if outcome == "capped":
            return self._trash_room() > 0
        return True                                 # wait, rescued

    def _process(self, ref: str, dups: DuplicateIndex, report: RunReport) -> None:
        ticket = self._ticket(ref)
        self.ledger.append({"kind": "start", "ref": ref, "ticket": ticket, "run": self._run_id})
        m = self.mailbox.fetch(ref)
        fps = fingerprints(m, self.fp)
        prev = self.ledger.decision(ref) or {}
        wait_since = prev.get("wait_since") if prev.get("outcome") == "wait" else None
        reason = exemption(m, owners=self.owners, rules=self.rules)
        original = None if reason else dups.original_of(ref, fps)
        if reason:
            verdict = Verdict("kept", reason=reason)
        elif original is not None:
            verdict = duplicate_verdict(original)
        else:
            answer = self.classifier.classify(m)
            if answer.error == "unavailable":
                report.problem("classifier_unavailable")
            waited = wait_since is not None and \
                self.now().timestamp() - wait_since > self.config.wait_days * DAY
            verdict = decide(answer, m, config=self.config, rules=self.rules,
                             waited_too_long=waited)

        outcome, applied = verdict.outcome, []
        if outcome == "act":
            outcome = gate(verdict.action, stage=self.stage, received_day=self._day(m.received),
                           trash_since=self._trash_since(), trash_room=self._trash_room())
            if outcome == "apply":
                applied = self._apply(m, verdict)
                outcome = "done"
                self.state["last_acting_run"] = self._run_id
        row = self._row(m, ticket, verdict, outcome, applied, fps)
        if outcome == "wait":
            row["wait_since"] = wait_since or round(self.now().timestamp(), 3)
        self.ledger.append(row)
        if ["trash"] not in applied:
            dups.add(ticket, ref, fps)
        report.items.append(self._item(m, ticket, outcome, verdict))

    def _failed(self, ref: str, err: Exception, report: RunReport) -> None:
        report.problem("mailbox" if isinstance(err, MailboxError) else "error")
        self.ledger.append({"kind": "error", "ref": ref, "ticket": self._ticket(ref),
                            "what": type(err).__name__})       # the type only, never a message
        if self.ledger.failures(ref) >= MAX_TRIES:
            self._give_up(ref, report)

    def _give_up(self, ref: str, report: RunReport) -> None:
        ticket = self._ticket(ref)
        self.ledger.append({"kind": "decision", "ref": ref, "ticket": ticket,
                            "run": self._run_id, "stage": str(self.stage), "outcome": "failed",
                            "wanted": "keep", "reason": "failed", "applied": []})
        report.items.append(Item(ticket=ticket, outcome="failed", reason="failed", ref=ref))

    # -- changing the mailbox -------------------------------------------------------------------

    def _apply(self, m: Message, verdict: Verdict) -> list[list[str]]:
        if verdict.action is Action.TRASH:
            # Counted and saved *before* the call: a crash can only leave the count too high
            # (fewer trashes that day), never too low.
            self._count_trash()
            self.mailbox.trash(m.ref)
            return [["trash"]]
        if verdict.action is Action.LABEL:
            return self._change(m, add=[verdict.label or ""])
        if verdict.action is Action.ARCHIVE:
            return self._change(m, add=[verdict.label or ""], remove=[INBOX])
        return []

    def _change(self, m: Message, add: list[str], remove: list[str] = ()) -> list[list[str]]:
        """One modify call with only the labels that actually change, recorded as ops, so an
        undo restores exactly the state before (a label the owner had already set stays).
        Filing adds labels of its own, plus INBOX for a rescue - never another system label:
        TRASH or SPAM here would be deleting by another name, past the caps and the backlog."""
        adding = tuple(lb for lb in add if lb and lb not in m.labels)
        removing = tuple(lb for lb in remove if lb in m.labels)
        if any(is_system_label(lb) and lb != INBOX for lb in adding):
            raise UnsafeChange("a filing change may not add a system label")
        if adding or removing:
            self.mailbox.modify(m.ref, add=adding, remove=removing)
        return [["label", lb] for lb in adding] + [["unlabel", lb] for lb in removing]

    def _count_trash(self) -> None:
        today = self._today().isoformat()
        if self.state.get("trash_day") != today:
            self.state["trash_day"], self.state["trash_today"] = today, 0
        self.state["trash_today"] = int(self.state["trash_today"]) + 1
        self._trashed_this_run += 1
        self.state.save()

    def _trash_room(self) -> int:
        used = int(self.state.get("trash_today", 0)) \
            if self.state.get("trash_day") == self._today().isoformat() else 0
        return min(self.config.trash_per_run - self._trashed_this_run,
                   self.config.trash_per_day - used)

    def _update_trash_since(self) -> None:
        """Auto-trash only covers mail that arrived since trashing was (last) switched on."""
        if self.stage < Stage.TRASH:
            self.state.pop("trash_since", None)
        else:
            self.state.setdefault("trash_since", self._today().isoformat())

    def _trash_since(self) -> date | None:
        """The backlog line: the day trash was (last) switched on. `trash_after` can move it
        later, never earlier - an early date would wave the whole lookback window through."""
        since = self.state.get("trash_since")
        if not since:
            return None
        line, after = date.fromisoformat(since), self.config.trash_after
        return line if after is None else max(line, after)

    # -- the trash backlog ----------------------------------------------------------------------

    def trash_backlog(self, approve: int) -> tuple[int, int]:
        """Trash the messages held back because they arrived before trashing was switched
        on - only when `approve` is exactly how many are waiting, never beyond the caps, and
        only those that still look the same (still in the inbox, not starred, no new rule).
        Returns (trashed, still waiting)."""
        if self.stage < Stage.TRASH:
            raise StageTooLow("trashing the backlog needs --max-stage trash")
        self._check_account()
        self._run_id = self._new_run_id("b")
        rows = sorted((r for r in self.ledger.decisions() if r.get("outcome") == "backlog"
                       and not self.ledger.is_final(r["ref"])), key=lambda r: r["ticket"])
        if len(rows) != approve:
            raise BacklogMismatch(f"{len(rows)} messages are waiting, not {approve}")
        valid: list[tuple[dict[str, Any], Message]] = []
        for row in rows:
            try:
                m = self.mailbox.fetch(row["ref"])
            except NotFound:
                m = None
            if m is None or m.box != "inbox" or exemption(m, owners=self.owners,
                                                          rules=self.rules) \
                    or self.rules.never_trash(m.sender, row.get("category")):
                self._redo(row, "kept", "changed_since", [])
            else:
                valid.append((row, m))
        trashed = 0
        for row, m in valid:
            if self._trash_room() <= 0:
                break
            applied = self._apply(m, Verdict("act", Action.TRASH))
            self._redo(row, "done", "backlog_approved", applied)
            trashed += 1
        if trashed:
            self.state["last_acting_run"] = self._run_id
        self.state.save()
        return trashed, len(valid) - trashed

    def _redo(self, row: dict[str, Any], outcome: str, reason: str,
              applied: list[list[str]]) -> None:
        fresh = dict(row)                           # append() gives it a new stamp
        fresh.update(run=self._run_id, stage=str(self.stage), outcome=outcome, reason=reason,
                     applied=applied)
        self.ledger.append(fresh)

    # -- operator commands and status ------------------------------------------------------------

    def command(self, text: str) -> list[str]:
        """The operator's way to run the same commands as a reply, from the CLI."""
        self._check_account()
        commands, unknown = parse_commands(text)
        ctx = CommandContext(self.mailbox, self.ledger, self.state, self.rules)
        notes = [line for cmd in commands for line in apply(cmd, ctx)]
        if unknown:
            notes.append(f"not understood: {len(unknown)} line(s)")
        self.state["pending_notes"] = list(self.state.get("pending_notes", [])) + notes
        self.state.save()
        return notes

    # -- digest and bookkeeping -----------------------------------------------------------------

    def _send_digest(self, report: RunReport) -> None:
        composed = compose(report, today=self._today(), trash_per_day=self.config.trash_per_day)
        if composed is None:
            self.state.pop("pending_notes", None)
            return
        subject, body = composed
        message_id = f"<janitor.{report.run_id}.{secrets.token_hex(6)}@{DIGEST_DOMAIN}>"
        try:
            ref = self.mailbox.send(Outgoing(sender=self.config.owner, to=self.config.owner,
                                             subject=subject, body=body,
                                             message_id=message_id, date=self.now()))
        except MailboxError:
            self.state["pending_notes"] = report.notes
            self.state["digest_failed"] = True
            return
        # Some providers replace the Message-ID of mail sent through their API; replies then
        # reference *their* id. Remember the id as stored, as well as ours.
        ids = [message_id]
        try:
            stored = self.mailbox.fetch(ref).message_id
            if stored and stored != message_id:
                ids.append(stored)
        except MailboxError:
            pass
        self.state["digests"] = [*self.state.get("digests", []), *ids][-KEEP_DIGESTS:]
        self.state["digest_refs"] = [*self.state.get("digest_refs", []), ref][-KEEP_DIGESTS:]
        self.state.pop("pending_notes", None)
        report.digest_sent = True

    def _finish(self, report: RunReport) -> None:
        line = summary_line(report)
        self.state["last_run"] = {"t": round(self.now().timestamp()), "run": report.run_id,
                                  "stage": str(report.stage), "counts": dict(report.counts())}
        self.state.save()
        when = self.now().astimezone(UTC).isoformat(timespec="seconds")
        append_private(self.state_dir / "run.log", f"{when} {line}\n")

    def _row(self, m: Message, ticket: int, verdict: Verdict, outcome: str,
             applied: list[list[str]], fps=None) -> dict[str, Any]:
        fps = fps or fingerprints(m, self.fp)
        return {"kind": "decision", "ref": m.ref, "ticket": ticket, "run": self._run_id,
                "stage": str(self.stage), "box": m.box, "outcome": outcome,
                "wanted": verdict.action.value, "reason": verdict.reason,
                "category": verdict.category, "confidence": round(verdict.confidence, 4),
                "source": verdict.source, "label": verdict.label, "applied": applied,
                "received": round(m.received.timestamp()),
                "fp": {"sender": fps.sender, "msgid": fps.msgid, "content": fps.content}}

    @staticmethod
    def _item(m: Message, ticket: int, outcome: str, verdict: Verdict) -> Item:
        return Item(ticket=ticket, outcome=outcome, action=verdict.action, reason=verdict.reason,
                    category=verdict.category, confidence=verdict.confidence,
                    source=verdict.source, label=verdict.label, sender=m.sender,
                    sender_name=m.sender_name, subject=m.subject, ref=m.ref)

    def _ticket(self, ref: str) -> int:
        known = self.ledger.ticket_of(ref)
        if known is not None:
            return known
        ticket = max(int(self.state.get("next_ticket", 1)), self.ledger.max_ticket() + 1)
        self.state["next_ticket"] = ticket + 1
        return ticket

    def _since(self) -> datetime:
        return self.now() - timedelta(days=self.config.lookback_days)

    def _today(self) -> date:
        return self.now().astimezone(self.tz).date()

    def _day(self, moment: datetime) -> date:
        return moment.astimezone(self.tz).date()


def read_status(config: Config, state_dir: Path | str,
                now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> dict[str, Any]:
    """Counts and settings only - nothing about any message. Needs no mailbox or credentials."""
    state_dir = Path(state_dir)
    state = State(state_dir / "state.json")
    ledger = Ledger(state_dir / "ledger.jsonl", now=lambda: now().timestamp(),
                    keep_days=config.keep_days)
    outcomes: dict[str, int] = {}
    for row in ledger.decisions():
        if not ledger.is_final(row["ref"]):
            outcome = str(row.get("outcome", "?"))
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
    today = now().astimezone(ZoneInfo(config.timezone)).date().isoformat()
    return {"paused": bool(state.get("paused")), "last_run": state.get("last_run"),
            "trash_since": state.get("trash_since"),
            "trashed_today": state.get("trash_today", 0) if state.get("trash_day") == today
            else 0, "outcomes": dict(sorted(outcomes.items()))}


def summary_line(report: RunReport) -> str:
    """Counts only: safe for cron mail, journald and CI logs."""
    counts = " ".join(f"{k}={v}" for k, v in sorted(report.counts().items())) or "nothing=0"
    flags = "".join(f" {flag}" for flag, on in (("paused", report.paused),
                                                ("halted", report.halted)) if on)
    return f"{report.run_id} stage={report.stage} {counts}{flags}"
