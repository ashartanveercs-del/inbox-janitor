"""Plug in your own classifier, or a model behind any chat-completions endpoint.

A classifier is anything with `name` and `classify(message) -> Classification`. Whatever it
returns is checked (a known category, a confidence in [0, 1]) and then goes through the same
policy, confidence floor, stage gate and caps as everything else - a classifier can't ask for
an action, only describe the message.

This example chains three: a domain rule, the built-in rules, then an "LLM" that is a stub
function here (swap in OpenAICompatibleChat(...) for a real one). Offline, no keys.

    python examples/custom_classifier.py
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta

from inbox_janitor import (
    Classification,
    Config,
    FirstConfident,
    Janitor,
    LLMClassifier,
    MemoryMailbox,
    Message,
    RuleClassifier,
    Stage,
)

OWNER = "owner@harborlane.example.com"


class CateringRequests:
    """A bakery's own rule: a customer asking about catering is personal mail, always."""

    name = "catering"

    def classify(self, m: Message) -> Classification:
        if "catering" in m.subject.lower():
            return Classification("personal", 0.99, self.name)
        return Classification("other", 0.0, self.name)


def stub_model(messages) -> str:
    """Stands in for OpenAICompatibleChat(base_url=..., model=..., api_key=...)."""
    email_text = messages[1]["content"].lower()
    if "webinar" in email_text:
        return json.dumps({"category": "promotion", "confidence": 0.98})
    return json.dumps({"category": "other", "confidence": 0.4})


def main() -> None:
    hour_ago = datetime.now(UTC) - timedelta(hours=1)
    inbox = [
        Message(ref="a1", sender="ana.lopez@corner-cafe.example.com", received=hour_ago,
                subject="Catering for 40 on Friday?", body="Two dozen focaccia, please."),
        Message(ref="a2", sender="events@bakeschool.example.com", received=hour_ago,
                subject="Free webinar: laminated doughs", body="Save your seat for Tuesday."),
        Message(ref="a3", sender="news@flour.example.com", received=hour_ago,
                subject="Mill news", body="Rye season is here.",
                headers={"list-unsubscribe": "<mailto:unsubscribe@flour.example.com>"}),
    ]
    classifier = FirstConfident([CateringRequests(), RuleClassifier(), LLMClassifier(stub_model)])
    with tempfile.TemporaryDirectory() as state:
        report = Janitor(mailbox=MemoryMailbox(OWNER, inbox), classifier=classifier,
                         config=Config.from_dict({"owner": OWNER}), pinned_stage=Stage.FILE,
                         state_dir=state).run()
    for item in sorted(report.items, key=lambda i: i.ticket):
        print(f"#{item.ticket}  {item.subject:<34} -> {item.category} {item.confidence:.2f} "
              f"(decided by {item.source}): {item.outcome} {item.action.value}")


if __name__ == "__main__":
    main()
