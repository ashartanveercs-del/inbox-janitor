"""inbox-janitor: staged-autonomy email triage.

An email janitor that earns its permissions (trial -> file -> trash) instead of being handed
them: the stage is pinned outside anything a model can touch, actions need 0.95 confidence,
trash is capped and held back for anything older than the day it was switched on, every
change can be undone by replying to the digest, and the ledger keeps keyed fingerprints -
never subjects, senders or bodies.
"""

from .classify import CATEGORIES, Classifier
from .classify.chain import FirstConfident
from .classify.llm import ClassifierUnavailable, LLMClassifier, OpenAICompatibleChat
from .classify.rules import DEFAULT_RULES, Rule, RuleClassifier
from .config import CONFIDENCE_FLOOR, Config, ConfigError, load_config
from .janitor import (
    BacklogMismatch,
    Janitor,
    StageTooLow,
    WrongAccount,
    read_status,
    summary_line,
)
from .mailbox import Mailbox, MailboxError, NotFound, Outgoing
from .mailbox.gmail import GmailMailbox, OAuthToken
from .mailbox.memory import JsonFileMailbox, MemoryMailbox
from .models import Attachment, Classification, Message
from .report import Item, RunReport
from .stages import REQUIRED_STAGE, Action, Stage

__version__ = "0.1.0"

__all__ = [
    "CATEGORIES", "CONFIDENCE_FLOOR", "DEFAULT_RULES", "REQUIRED_STAGE", "Action", "Attachment",
    "BacklogMismatch", "Classification", "Classifier", "ClassifierUnavailable", "Config",
    "ConfigError", "FirstConfident", "GmailMailbox", "Item", "Janitor", "JsonFileMailbox",
    "LLMClassifier", "Mailbox", "MailboxError", "MemoryMailbox", "Message", "NotFound",
    "OAuthToken", "OpenAICompatibleChat", "Outgoing", "Rule", "RuleClassifier", "RunReport",
    "Stage", "StageTooLow", "WrongAccount", "load_config", "read_status", "summary_line",
]
