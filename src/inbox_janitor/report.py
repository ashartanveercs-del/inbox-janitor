"""What one run did, held in memory for the digest. Never written to disk: it is the only
place subjects and senders appear, and it goes straight back into the owner's own mailbox."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .stages import Action, Stage


@dataclass(frozen=True)
class Item:
    ticket: int
    outcome: str                  # done | suggested | review | wait | kept | capped | backlog |
    action: Action = Action.KEEP  # failed | rescued
    reason: str = ""
    category: str | None = None
    confidence: float = 0.0
    source: str = ""              # which classifier answered
    label: str | None = None
    sender: str = ""
    sender_name: str = ""
    subject: str = ""
    ref: str = ""


@dataclass
class RunReport:
    run_id: str
    stage: Stage
    paused: bool = False
    items: list[Item] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)       # results of the owner's commands
    problems: list[str] = field(default_factory=list)    # codes, see digest.PROBLEMS
    left_in_spam: int = 0
    unauthenticated: int = 0      # trusted senders whose spam failed authentication
    halted: bool = False
    digest_sent: bool = False

    def counts(self) -> Counter[str]:
        c: Counter[str] = Counter(i.outcome for i in self.items)
        c["trashed"] = sum(1 for i in self.items if i.outcome == "done"
                           and i.action is Action.TRASH)
        c["filed"] = sum(1 for i in self.items if i.outcome == "done"
                         and i.action in (Action.LABEL, Action.ARCHIVE))
        return +c

    def problem(self, code: str) -> None:
        if code not in self.problems:
            self.problems.append(code)
