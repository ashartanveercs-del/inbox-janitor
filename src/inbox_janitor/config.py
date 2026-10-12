"""Operator configuration (TOML), validated in one pass so every problem is reported at once.

What the config can and can't do is part of the safety model:
  * `stage` can only *lower* the `--max-stage` pin given on the command line;
  * `min_confidence` can only *raise* the 0.95 floor (lower values are clamped, with a warning);
  * caps have hard ceilings in code; `personal` mail can never be mapped to `trash`;
  * a label can never be a system label (`TRASH`, `SPAM`, ...): filing must not delete;
  * secrets never live here: the LLM key is read from the environment variable it names.
"""

from __future__ import annotations

import math
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

from .addresses import is_sender_pattern
from .classify import CATEGORIES
from .classify.rules import Rule
from .models import is_system_label
from .stages import Action, Stage

CONFIDENCE_FLOOR = 0.95
MAX_TRASH_PER_RUN = 200
MAX_TRASH_PER_DAY = 500
NEVER_TRASHED = frozenset({"personal"})
POLICY_ACTIONS = (Action.KEEP, Action.LABEL, Action.ARCHIVE, Action.TRASH)
CLASSIFIER_KINDS = ("rules", "llm", "rules+llm")
DUPLICATE_LABEL = "Janitor/Duplicates"

DEFAULT_POLICY: Mapping[str, Action] = MappingProxyType({
    "invoice": Action.LABEL,
    "receipt": Action.ARCHIVE,
    "contract": Action.LABEL,
    "support": Action.LABEL,
    "newsletter": Action.ARCHIVE,
    "promotion": Action.TRASH,
    "notification": Action.ARCHIVE,
    "personal": Action.KEEP,
    "other": Action.KEEP,
})

DEFAULT_LABELS: Mapping[str, str] = MappingProxyType({
    "invoice": "Janitor/Invoices", "receipt": "Janitor/Receipts",
    "contract": "Janitor/Contracts", "support": "Janitor/Support",
    "newsletter": "Janitor/Newsletters", "promotion": "Janitor/Promotions",
    "notification": "Janitor/Notifications", "personal": "Janitor/Personal",
    "other": "Janitor/Other",
})

_RULE_KEYS = ("category", "confidence", "senders", "subject_any", "body_any", "headers_any",
              "attachment_ext_any")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ClassifierConfig:
    kind: str = "rules"
    model: str = ""
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "INBOX_JANITOR_LLM_KEY"


@dataclass(frozen=True)
class Config:
    owner: str
    stage: Stage | None = None
    min_confidence: float = CONFIDENCE_FLOOR
    lookback_days: int = 30
    trash_per_run: int = 20
    trash_per_day: int = 50
    trash_after: date | None = None
    wait_days: int = 3
    keep_days: int = 180
    timezone: str = "UTC"
    policy: Mapping[str, Action] = DEFAULT_POLICY
    labels: Mapping[str, str] = DEFAULT_LABELS
    trusted_senders: tuple[str, ...] = ()
    rules: tuple[Rule, ...] = ()
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    warnings: tuple[str, ...] = ()

    def label_for(self, category: str) -> str:
        return self.labels.get(category) or DEFAULT_LABELS[category]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Config:
        return _Parser(data).build()


def load_config(path: Path | str) -> Config:
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as err:
        raise ConfigError(f"can't read {path}: {err.strerror}") from None
    except tomllib.TOMLDecodeError as err:
        raise ConfigError(f"{path} is not valid TOML: {err}") from None
    return Config.from_dict(data)


class _Parser:
    def __init__(self, data: Mapping[str, Any]) -> None:
        self.data = data
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.out: dict[str, Any] = {}

    def build(self) -> Config:
        known = {f for f in Config.__dataclass_fields__ if f != "warnings"}
        for key in self.data:
            if key not in known:
                self.errors.append(f"unknown setting {key!r}")
        self._owner()
        self._stage()
        self._floor()
        for name, lo, hi in (("lookback_days", 1, 3650), ("wait_days", 0, 365),
                             ("keep_days", 1, 3650), ("trash_per_run", 0, MAX_TRASH_PER_RUN),
                             ("trash_per_day", 0, MAX_TRASH_PER_DAY)):
            self._int(name, lo, hi)
        self._relations()
        self._trash_after()
        self._timezone()
        self._policy()
        self._labels()
        self._trusted()
        self._rules()
        self._classifier()
        if self.errors:
            raise ConfigError("invalid config: " + "; ".join(self.errors))
        return Config(**self.out, warnings=tuple(self.warnings))

    def _owner(self) -> None:
        owner = self.data.get("owner")
        if not isinstance(owner, str) or "@" not in owner or not is_sender_pattern(owner):
            self.errors.append("owner must be the mailbox's email address")
        else:
            self.out["owner"] = owner.strip().lower()

    def _stage(self) -> None:
        if "stage" in self.data:
            try:
                self.out["stage"] = Stage.parse(self.data["stage"])
            except ValueError as err:
                self.errors.append(f"stage: {err}")

    def _floor(self) -> None:
        if "min_confidence" not in self.data:
            return
        value = self.data["min_confidence"]
        if isinstance(value, bool) or not isinstance(value, int | float) \
                or not math.isfinite(value) or not 0 <= value <= 1:
            self.errors.append("min_confidence must be a number from 0.95 to 1")
            return
        if value < CONFIDENCE_FLOOR:
            self.warnings.append(f"min_confidence {value} is below the {CONFIDENCE_FLOOR} "
                                 f"floor; using {CONFIDENCE_FLOOR}")
            value = CONFIDENCE_FLOOR
        self.out["min_confidence"] = float(value)

    def _int(self, name: str, lo: int, hi: int) -> None:
        if name not in self.data:
            return
        value = self.data[name]
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            self.errors.append(f"{name} must be a whole number from {lo} to {hi}")
        else:
            self.out[name] = value

    def _relations(self) -> None:
        per_run = self.out.get("trash_per_run", Config.trash_per_run)
        per_day = self.out.get("trash_per_day", Config.trash_per_day)
        if per_run > per_day:
            self.errors.append(f"trash_per_run ({per_run}) can't exceed trash_per_day "
                               f"({per_day})")
        lookback = self.out.get("lookback_days", Config.lookback_days)
        if self.out.get("keep_days", Config.keep_days) <= lookback:
            self.errors.append("keep_days must be longer than lookback_days, or old messages "
                               "would be processed twice")

    def _trash_after(self) -> None:
        if "trash_after" not in self.data:
            return
        value = self.data["trash_after"]
        if isinstance(value, date):
            self.out["trash_after"] = value
            return
        try:
            self.out["trash_after"] = date.fromisoformat(str(value))
        except ValueError:
            self.errors.append("trash_after must be a date like 2026-10-01")

    def _timezone(self) -> None:
        if "timezone" not in self.data:
            return
        name = self.data["timezone"]
        try:
            ZoneInfo(str(name))
        except Exception:
            self.errors.append(f"timezone {name!r} is not a known IANA zone")
        else:
            self.out["timezone"] = str(name)

    def _policy(self) -> None:
        raw = self.data.get("policy", {})
        if not isinstance(raw, Mapping):
            self.errors.append("policy must be a table of category = action")
            return
        policy = dict(DEFAULT_POLICY)
        for category, action in raw.items():
            if category not in CATEGORIES:
                self.errors.append(f"policy: unknown category {category!r}")
                continue
            if action not in [a.value for a in POLICY_ACTIONS]:
                self.errors.append(f"policy.{category}: unknown action {action!r} (use one of "
                                   f"{', '.join(a.value for a in POLICY_ACTIONS)})")
                continue
            if category in NEVER_TRASHED and action == Action.TRASH:
                self.errors.append(f"policy.{category}: personal mail is never trashed "
                                   "automatically")
                continue
            policy[category] = Action(action)
        self.out["policy"] = MappingProxyType(policy)

    def _labels(self) -> None:
        raw = self.data.get("labels", {})
        if not isinstance(raw, Mapping):
            self.errors.append("labels must be a table of category = label name")
            return
        labels = dict(DEFAULT_LABELS)
        for category, name in raw.items():
            if category not in CATEGORIES or not isinstance(name, str) or not name.strip():
                self.errors.append(f"labels: bad entry {category!r}")
            elif is_system_label(name):
                self.errors.append(f"labels.{category}: {name.strip()!r} is a system label; "
                                   "filing only adds labels of its own")
            else:
                labels[category] = name.strip()
        self.out["labels"] = MappingProxyType(labels)

    def _trusted(self) -> None:
        raw = self.data.get("trusted_senders", [])
        if not isinstance(raw, list) or not all(isinstance(s, str) and is_sender_pattern(s)
                                                for s in raw):
            self.errors.append("trusted_senders must be a list of addresses or @domains")
            return
        self.out["trusted_senders"] = tuple(s.strip().lower() for s in raw)

    def _rules(self) -> None:
        raw = self.data.get("rules", [])
        if not isinstance(raw, list):
            self.errors.append("rules must be a list of tables")
            return
        rules = []
        for i, item in enumerate(raw):
            try:
                if not isinstance(item, Mapping) or set(item) - set(_RULE_KEYS):
                    raise ValueError(f"keys must be among {', '.join(_RULE_KEYS)}")
                args = {k: (tuple(v) if isinstance(v, list) else v) for k, v in item.items()}
                rules.append(Rule(**args))
            except (TypeError, ValueError) as err:
                self.errors.append(f"rules[{i}]: {err}")
        self.out["rules"] = tuple(rules)

    def _classifier(self) -> None:
        raw = self.data.get("classifier", {})
        if not isinstance(raw, Mapping) or set(raw) - set(ClassifierConfig.__dataclass_fields__):
            self.errors.append("classifier takes kind, model, base_url and api_key_env")
            return
        try:
            c = ClassifierConfig(**{k: str(v) for k, v in raw.items()})
        except TypeError as err:
            self.errors.append(f"classifier: {err}")
            return
        if c.kind not in CLASSIFIER_KINDS:
            self.errors.append(f"classifier kind {c.kind!r} must be one of "
                               f"{', '.join(CLASSIFIER_KINDS)}")
        elif "llm" in c.kind and not c.model:
            self.errors.append("classifier model is required when kind uses the llm")
        self.out["classifier"] = c
