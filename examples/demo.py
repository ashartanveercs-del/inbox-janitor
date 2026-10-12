"""Three days with the janitor, offline: trial -> file -> the owner replies -> trash -> backlog.

Drives the real command-line entry point against a temporary copy of examples/mailbox.json,
a fictional bakery's inbox. No network, no keys, nothing is sent anywhere: the "mailbox" is a
JSON file, and the digests it prints are the emails the janitor would send its owner.

    python examples/demo.py
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from inbox_janitor.cli import main

HERE = Path(__file__).resolve().parent
OWNER = "owner@harborlane.example.com"
# Mid-afternoon in the config's timezone, so "yesterday" and "today" are never ambiguous.
DAY1 = datetime.now(UTC).replace(hour=19, minute=0, second=0, microsecond=0)


def banner(text: str) -> None:
    print("\n" + "=" * 78 + f"\n{text}\n" + "=" * 78)


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def latest_digest(path: Path) -> dict | None:
    sent = [m for m in load(path)["messages"]
            if "SENT" in m.get("labels", []) and m["subject"].startswith("Inbox janitor")]
    return sent[-1] if sent else None


def show_digest(path: Path) -> None:
    digest = latest_digest(path)
    print(f"\nSubject: {digest['subject']}\n\n{digest['body']}" if digest
          else "\n(no digest: nothing worth an email)")


def ticket(path: Path, words: str) -> int:
    """The ticket number the digest shows for the first line mentioning `words`."""
    lines = latest_digest(path)["body"].splitlines()
    line = next(x for x in lines if words in x and "copy of" not in x)
    return int(re.search(r"#(\d+)", line).group(1))


def add(path: Path, message: dict) -> None:
    data = load(path)
    data["messages"].append(message)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def reply(path: Path, text: str, when: datetime) -> None:
    """The owner answers the latest digest from their own account (so it carries SENT)."""
    digest = latest_digest(path)
    print("\nThe owner replies:\n  " + text.replace("\n", "\n  "))
    add(path, {"ref": f"reply-{when:%d%H%M}", "from": OWNER, "to": [OWNER],
               "received": when.isoformat(), "subject": "Re: " + digest["subject"],
               "body": text + "\n\nOn the day before, the janitor wrote:\n> "
                       + digest["body"].replace("\n", "\n> "),
               "labels": ["SENT"], "message_id": f"<reply-{when:%d%H%M}@harborlane.example.com>",
               "in_reply_to": digest["message_id"], "references": [digest["message_id"]]})


def cli(base: list[str], *args: str, when: datetime) -> None:
    print(f"\n$ inbox-janitor {' '.join(args)}")
    code = main([*base, *args], env={}, now=lambda: when)
    if code:
        sys.exit(code)


def demo() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        mailbox = Path(tmp) / "mailbox.json"
        shutil.copy(HERE / "mailbox.json", mailbox)
        base = ["--config", str(HERE / "janitor.example.toml"),
                "--state-dir", str(Path(tmp) / "state"), "--mailbox", f"json:{mailbox}"]

        banner("Day 1 - trial: classify and report; the only change is rescuing from Spam")
        cli(base, "run", "--max-stage", "trial", when=DAY1)
        show_digest(mailbox)

        day2 = DAY1 + timedelta(days=1)
        banner("Day 2 - the operator raises the pin to 'file': labels and archiving")
        cli(base, "run", "--max-stage", "file", when=day2)
        show_digest(mailbox)
        newsletter = ticket(mailbox, "rye season")
        reply(mailbox, f"undo {newsletter}\nnever touch news@flour.example.com",
              when=day2 + timedelta(hours=2))

        day3 = DAY1 + timedelta(days=2)
        banner("Day 3 - the pin goes to 'trash'; a fresh promotion has just arrived")
        add(mailbox, {"ref": "m12", "from": "Oven Outlet <deals@ovens.example.com>",
                      "received": (day3 - timedelta(hours=1)).isoformat(),
                      "subject": "Weekend deal: 25% off dough scrapers",
                      "body": "This weekend only, every scraper is 25% off.",
                      "headers": {"List-Unsubscribe": "<https://ovens.example.com/unsub>"},
                      "labels": ["INBOX", "UNREAD"]})
        cli(base, "run", "--max-stage", "trash", when=day3)
        show_digest(mailbox)

        waiting = re.search(r"--approve (\d+)", latest_digest(mailbox)["body"])
        if waiting:
            banner("The operator approves the held-back trash, quoting the exact count")
            cli(base, "trash-backlog", "--approve", waiting.group(1), "--max-stage", "trash",
                when=day3 + timedelta(minutes=5))

        banner("Status (counts only)")
        cli(base, "status", when=day3 + timedelta(minutes=6))
        state_files = sorted(p.name for p in (Path(tmp) / "state").iterdir())
        print(f"\nState on disk: {', '.join(state_files)} - keyed fingerprints, ids and counts;"
              " no subjects, senders or bodies.")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):          # subjects can be any language
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    demo()
