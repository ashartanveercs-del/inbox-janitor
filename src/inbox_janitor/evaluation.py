"""Measure a classifier against labelled mail: what the confidence floor actually buys.

A classifier's confidence is its own claim - a rule's fixed number, or a model's self-report -
and the floor is a gate on that claim. Neither is a probability until it is measured. For
each category this reports how often an answer at or above the floor was right, with a 95%
Wilson lower bound (a small corpus can't promise more than that), how stated confidence
compares with accuracy, and the two numbers that matter most under the default policy: mail
that would be trashed by mistake, and mail the policy keeps (personal, other) that would leave
the inbox.

Only message ids are reported, never subjects or addresses, so the same report can be run on
a labelled export of a real mailbox.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .classify import CATEGORIES, Classifier
from .config import CONFIDENCE_FLOOR, DEFAULT_POLICY
from .mailbox.memory import message_from_json
from .models import Message
from .stages import Action

# stated-confidence bins for the reliability table: [low, high)
BINS = ((0.0, 0.5), (0.5, 0.8), (0.8, 0.9), (0.9, 0.95), (0.95, 0.97), (0.97, 0.99),
        (0.99, 1.01))
LEAVES_INBOX = frozenset({Action.ARCHIVE, Action.TRASH})


@dataclass(frozen=True)
class Labelled:
    id: str
    message: Message
    label: str


def load_corpus(path: Path | str) -> tuple[str, list[Labelled]]:
    """(owner, labelled messages) from the demo mailbox's JSON format plus a `label` each."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    now = datetime.now(UTC)
    corpus = []
    for item in data["messages"]:
        if item.get("label") not in CATEGORIES:
            raise ValueError(f"{item.get('ref')}: label must be one of {', '.join(CATEGORIES)}")
        message = message_from_json({"received": "-1h", **item}, now)
        corpus.append(Labelled(str(item["ref"]), message, item["label"]))
    if len({c.id for c in corpus}) != len(corpus):
        raise ValueError("message refs must be unique")
    return str(data["owner"]), corpus


def wilson_lower(right: int, n: int, z: float = 1.96) -> float:
    """The lower end of the 95% Wilson score interval for right/n: with 15 of 15 right, the
    true rate could still be as low as 0.80."""
    if n == 0:
        return 0.0
    p = right / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / (1 + z * z / n)


@dataclass
class Score:
    category: str
    labelled: int = 0          # messages whose true label is this category
    answered: int = 0          # answers of this category at or above the floor
    right: int = 0             # ... that were right

    @property
    def precision(self) -> float | None:
        return self.right / self.answered if self.answered else None

    @property
    def lower(self) -> float | None:
        return wilson_lower(self.right, self.answered) if self.answered else None

    @property
    def recall(self) -> float | None:
        return self.right / self.labelled if self.labelled else None


@dataclass
class Evaluation:
    classifier: str
    floor: float
    scores: dict[str, Score] = field(default_factory=dict)
    bins: list[list[int]] = field(default_factory=lambda: [[0, 0] for _ in BINS])
    total: int = 0
    unusable: int = 0                                     # invalid or unavailable answers
    acted: int = 0                                        # at the floor, policy isn't keep
    wrong: list[tuple[str, str, str]] = field(default_factory=list)   # (id, label, answer)
    trashed_by_mistake: list[str] = field(default_factory=list)
    kept_mail_moved: list[str] = field(default_factory=list)

    @property
    def answered(self) -> int:
        return sum(s.answered for s in self.scores.values())

    @property
    def right(self) -> int:
        return sum(s.right for s in self.scores.values())


def evaluate(classifier: Classifier, corpus: Sequence[Labelled], *,
             floor: float = CONFIDENCE_FLOOR,
             policy: Mapping[str, Action] = DEFAULT_POLICY) -> Evaluation:
    ev = Evaluation(classifier.name, floor, {c: Score(c) for c in CATEGORIES})
    for item in corpus:
        ev.total += 1
        ev.scores[item.label].labelled += 1
        got = classifier.classify(item.message)
        if got.error is not None or got.category is None:
            ev.unusable += 1
            continue
        hit = got.category == item.label
        for slot, (low, high) in zip(ev.bins, BINS, strict=True):
            if low <= got.confidence < high:
                slot[0] += 1
                slot[1] += hit
        if got.confidence < floor:
            continue
        score = ev.scores[got.category]
        score.answered += 1
        score.right += hit
        if not hit:
            ev.wrong.append((item.id, item.label, got.category))
        action = policy[got.category]
        if action is not Action.KEEP:
            ev.acted += 1
        if action is Action.TRASH and policy[item.label] is not Action.TRASH:
            ev.trashed_by_mistake.append(item.id)
        if action in LEAVES_INBOX and policy[item.label] is Action.KEEP:
            ev.kept_mail_moved.append(item.id)
    return ev


def _num(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _ids(ids: Sequence[str]) -> str:
    return ", ".join(f"`{i}`" for i in ids) if ids else "none"


def render(ev: Evaluation) -> str:
    """A markdown report: the per-category table, the safety numbers, the reliability table."""
    lines = [
        f"`{ev.classifier}` on {ev.total} labelled messages, floor {ev.floor:.2f}:",
        "",
        f"| category | labelled | answered at >= {ev.floor:.2f} | right | precision "
        "| 95% lower bound | recall |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for s in ev.scores.values():
        lines.append(f"| {s.category} | {s.labelled} | {s.answered} | {s.right} "
                     f"| {_num(s.precision)} | {_num(s.lower)} | {_num(s.recall)} |")
    precision = ev.right / ev.answered if ev.answered else None
    lower = wilson_lower(ev.right, ev.answered) if ev.answered else None
    lines += [
        f"| **all** | {ev.total} | {ev.answered} | {ev.right} | {_num(precision)} "
        f"| {_num(lower)} | {_num(ev.right / ev.total if ev.total else None)} |",
        "",
        f"- Acted on under the default policy: {ev.acted} of {ev.total}; the rest is left "
        "alone (kept, or reported for review).",
        f"- Trashed by mistake: {_ids(ev.trashed_by_mistake)}.",
        f"- Personal or other mail moved out of the inbox: {_ids(ev.kept_mail_moved)}.",
        "- Wrong at the floor: " + (", ".join(f"`{i}` ({label} answered {answer})"
                                             for i, label, answer in ev.wrong) or "none") + ".",
        f"- No usable answer: {ev.unusable}.",
        "",
        "| stated confidence | answers | right | accuracy |",
        "|---|---:|---:|---:|",
    ]
    for (low, high), (n, right) in zip(BINS, ev.bins, strict=True):
        if n:
            span = f"{low:.2f}-{min(high, 1.0):.2f}"
            lines.append(f"| {span} | {n} | {right} | {right / n:.2f} |")
    return "\n".join(lines) + "\n"
