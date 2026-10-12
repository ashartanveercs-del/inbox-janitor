"""From a classifier's answer to what may happen - as pure functions, so every rule is a
one-line test.

    exemption()  mail that is never touched, checked before any classifier is asked
    decide()     the operator's policy for the category, the confidence floor, owner rules
    gate()       whether the current stage, the trash backlog line and the caps allow it
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date

from .config import DUPLICATE_LABEL, Config
from .models import SENT, STARRED, Classification, Message
from .stages import Action, Stage, permits
from .userrules import UserRules


@dataclass(frozen=True)
class Verdict:
    outcome: str                    # "act" | "kept" | "review" | "wait"
    action: Action = Action.KEEP
    reason: str = ""
    label: str | None = None
    category: str | None = None
    confidence: float = 0.0
    source: str = ""


def exemption(m: Message, *, owners: Collection[str], rules: UserRules | None) -> str | None:
    if m.sender in owners or SENT in m.labels:
        return "own_mail"
    if STARRED in m.labels:
        return "starred"            # a human already said this one matters
    if rules is not None and rules.never_touch(m.sender):
        return "never_touch"
    return None


def duplicate_verdict(original_ticket: int) -> Verdict:
    """Deterministic (hash equality), so no confidence is involved; archiving is reversible."""
    return Verdict("act", Action.ARCHIVE, f"duplicate_of_{original_ticket}", DUPLICATE_LABEL)


def decide(c: Classification, m: Message, *, config: Config, rules: UserRules | None,
           waited_too_long: bool = False) -> Verdict:
    seen = {"category": c.category, "confidence": c.confidence, "source": c.source}
    if c.error == "unavailable":
        if waited_too_long:
            return Verdict("review", reason="classifier_unavailable", **seen)
        return Verdict("wait", reason="classifier_unavailable", **seen)
    if c.error is not None or c.category is None:
        return Verdict("review", reason="invalid_answer", **seen)
    wanted = config.policy[c.category]
    if wanted is Action.KEEP:
        return Verdict("kept", reason="policy", **seen)
    if c.confidence < config.min_confidence:
        return Verdict("review", reason="low_confidence", **seen)
    label = config.label_for(c.category)
    if wanted is Action.TRASH and rules is not None and rules.never_trash(m.sender, c.category):
        return Verdict("act", Action.ARCHIVE, "never_trash", label, **seen)
    return Verdict("act", wanted, "policy", label, **seen)


def gate(action: Action, *, stage: Stage, received_day: date, trash_since: date | None,
         trash_room: int) -> str:
    """'apply', or why not: 'suggested' (the stage doesn't permit it yet), 'backlog' (trash
    for mail that arrived before trash was enabled - needs the owner's explicit approval),
    'capped' (today's trash allowance is used up; tried again later)."""
    if not permits(stage, action):
        return "suggested"
    if action is Action.TRASH:
        if trash_since is None or received_day < trash_since:
            return "backlog"
        if trash_room <= 0:
            return "capped"
    return "apply"
