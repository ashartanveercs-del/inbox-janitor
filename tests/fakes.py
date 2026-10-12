"""Test doubles: a scripted classifier, a mailbox that fails on demand, a clock, and a World
that wires them into a Janitor the way the CLI does - one Janitor per run, sharing a state
directory, like separate scheduler invocations."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from factories import OWNER, T0

from inbox_janitor.classify.rules import RuleClassifier
from inbox_janitor.config import Config
from inbox_janitor.janitor import Janitor
from inbox_janitor.mailbox import MailboxError, Outgoing
from inbox_janitor.mailbox.memory import MemoryMailbox
from inbox_janitor.models import Classification, Message
from inbox_janitor.privacy import Fingerprinter
from inbox_janitor.report import RunReport
from inbox_janitor.stages import Stage

FP = Fingerprinter(b"k" * 32)
SERVER = "mx.example.com"


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.t = now

    def now(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)


class ScriptedClassifier:
    """Answers by subject; everything else is 'other' and unsure. Records what it was asked."""

    name = "scripted"

    def __init__(self, answers: dict[str, Classification] | None = None,
                 default: Classification | None = None) -> None:
        self.answers = dict(answers or {})
        self.default = default or Classification("other", 0.5, self.name)
        self.asked: list[str] = []

    def classify(self, message: Message) -> Classification:
        self.asked.append(message.ref)
        return self.answers.get(message.subject, self.default)


class FlakyMailbox(MemoryMailbox):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fail_fetch: set[str] = set()
        self.fail_trash = False
        self.fail_send = False
        self.address_override: str | None = None
        self.fetched: list[str] = []

    def address(self) -> str:
        return self.address_override or super().address()

    def fetch(self, ref: str) -> Message:
        self.fetched.append(ref)
        if ref in self.fail_fetch:
            raise MailboxError("fetch", 503)
        return super().fetch(ref)

    def trash(self, ref: str) -> None:
        if self.fail_trash:
            raise MailboxError("trash", 503)
        super().trash(ref)

    def send(self, message: Outgoing) -> str:
        if self.fail_send:
            raise MailboxError("send", 503)
        return super().send(message)


def auth_ok(domain: str, server: str = SERVER) -> str:
    return f"{server}; dkim=pass header.d={domain}; dmarc=pass header.from={domain}"


class World:
    def __init__(self, tmp_path: Path, *messages: Message, stage: Stage = Stage.TRIAL,
                 config: dict | None = None, classifier=None) -> None:
        self.clock = Clock()
        self.mailbox = FlakyMailbox(OWNER, messages, serv_id=SERVER, now=self.clock.now)
        self.state_dir = tmp_path / "state"
        self.config = Config.from_dict({"owner": OWNER, **(config or {})})
        self.stage = stage
        # like the command line: the owner's own domain is never bulk mail
        self.classifier = classifier or RuleClassifier(own_domains=(OWNER.split("@")[1],))

    def janitor(self, stage: Stage | None = None, classifier=None) -> Janitor:
        return Janitor(mailbox=self.mailbox, classifier=classifier or self.classifier,
                       config=self.config, pinned_stage=stage or self.stage,
                       state_dir=self.state_dir, now=self.clock.now, fp=FP)

    def run(self, stage: Stage | None = None, classifier=None) -> RunReport:
        return self.janitor(stage, classifier).run()

    def labels(self, m: Message) -> set[str]:
        return set(self.mailbox.fetch(m.ref).labels)

    def changes(self) -> list[tuple]:
        """Every write except sending digests."""
        return [w for w in self.mailbox.writes if w[0] != "send"]

    def digests(self) -> list[Outgoing]:
        return [o for o in self.mailbox.sent if o.subject.startswith("Inbox janitor")]

    def reply(self, text: str, *, sent: bool = True) -> str:
        """The owner answers the latest digest from their own account (`sent`), or a stranger
        forges one into the inbox (`sent=False`)."""
        digest = self.digests()[-1]
        self.clock.advance(minutes=5)
        ref = f"reply{len(self.mailbox.messages)}"
        self.mailbox.add(Message(
            ref=ref, sender=OWNER, subject="Re: " + digest.subject,
            body=text + "\n\nOn Mon, Oct 12, 2026 at 2:00 PM <" + OWNER + "> wrote:\n> "
            + digest.body.replace("\n", "\n> "),
            received=self.clock.now(), labels=frozenset({"SENT"} if sent else {"INBOX"}),
            message_id=f"<{ref}@mail.example.com>", in_reply_to=digest.message_id,
            references=(digest.message_id,)))
        self.clock.advance(minutes=5)
        return ref
