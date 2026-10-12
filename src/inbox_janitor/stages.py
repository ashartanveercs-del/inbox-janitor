"""Autonomy stages and the actions each one permits.

The janitor earns permissions in three steps instead of being handed them:

    trial  - classify and report what it *would* do; the only change it makes is pulling
             trusted, authenticated mail out of Spam (doing nothing there loses mail, because
             providers purge Spam after ~30 days)
    file   - also label and archive
    trash  - also move mail to the trash (capped per run and per day)

The stage is a ceiling pinned by the operator (the `--max-stage` flag in the scheduler entry).
Everything else - the config file, the classifier's output, reply commands - can only lower
it. Nothing in this module reads model output, so nothing a model says can widen it.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from types import MappingProxyType


class Stage(IntEnum):
    TRIAL = 0
    FILE = 1
    TRASH = 2

    @classmethod
    def parse(cls, text: str) -> Stage:
        name = str(text).strip().upper()
        if name not in cls.__members__:
            raise ValueError(f"unknown stage {text!r}: expected one of trial, file, trash")
        return cls[name]

    def __str__(self) -> str:
        return self.name.lower()


class Action(StrEnum):
    KEEP = "keep"        # leave the message exactly as it is
    RESCUE = "rescue"    # Spam -> Inbox
    LABEL = "label"      # add a label, stay in the inbox
    ARCHIVE = "archive"  # add a label and leave the inbox
    TRASH = "trash"      # move to the trash (recoverable for ~30 days)


# Read-only on purpose: the table is the whole permission model.
REQUIRED_STAGE = MappingProxyType({
    Action.KEEP: Stage.TRIAL,
    Action.RESCUE: Stage.TRIAL,
    Action.LABEL: Stage.FILE,
    Action.ARCHIVE: Stage.FILE,
    Action.TRASH: Stage.TRASH,
})


def permits(stage: Stage, action: Action) -> bool:
    return stage >= REQUIRED_STAGE[action]


def effective_stage(pinned: Stage, requested: Stage | None) -> Stage:
    """The operator's pin is a ceiling: a requested stage can lower it, never raise it."""
    return pinned if requested is None else min(pinned, requested)
