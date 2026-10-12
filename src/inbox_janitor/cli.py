"""inbox-janitor command line.

    inbox-janitor run --max-stage trial            one scheduled run
    inbox-janitor command "undo 3" "keep 5"        the reply commands, from the operator
    inbox-janitor status                           counts only, as JSON
    inbox-janitor trash-backlog --approve N --max-stage trash
    inbox-janitor check --max-stage file           what the config resolves to

`--max-stage` is the autonomy ceiling. Put it in the crontab / systemd unit, which the agent
can't edit: the config file can lower the stage, nothing can raise it.

Exit codes: 0 ok, 2 refused or misconfigured, 3 wrong mailbox account, 4 failed,
75 another run holds the lock. Output is counts and ticket numbers - never a subject or an
address - so cron mail and journald stay clean.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from .classify.chain import FirstConfident
from .classify.llm import LLMClassifier, OpenAICompatibleChat
from .classify.rules import DEFAULT_RULES, RuleClassifier
from .config import Config, ConfigError, load_config
from .janitor import (
    BacklogMismatch,
    Janitor,
    StageTooLow,
    WrongAccount,
    read_status,
    summary_line,
)
from .lock import RunLock
from .mailbox import Mailbox, MailboxError
from .mailbox.gmail import GmailMailbox, OAuthToken
from .mailbox.memory import JsonFileMailbox
from .net import HttpFn, urllib_send
from .stages import Stage, effective_stage

STAGES = ("trial", "file", "trash")
LLM_URL, LLM_KEY = "INBOX_JANITOR_LLM_URL", "INBOX_JANITOR_LLM_KEY"
DEFAULT_LLM_URL = "https://api.openai.com/v1"
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


class SetupError(Exception):
    """Something needed to start is missing (a token file, an API key)."""


def build_parser() -> argparse.ArgumentParser:
    # The shared options work before or after the subcommand: the subcommand copies default to
    # SUPPRESS, so they only override the top-level values when actually given there.
    common = argparse.ArgumentParser(add_help=False)
    for option in ("--config", "--state-dir", "--mailbox", "--gmail-token", "--env-file"):
        common.add_argument(option, default=argparse.SUPPRESS)

    top = argparse.ArgumentParser(prog="inbox-janitor", description=__doc__.split("\n")[0])
    top.add_argument("--config", default="janitor.toml", help="operator config (TOML)")
    top.add_argument("--state-dir", default=None,
                     help="ledger, state and key (default $INBOX_JANITOR_STATE or "
                          "~/.inbox-janitor)")
    top.add_argument("--mailbox", default="gmail", help="gmail (default) or json:PATH")
    top.add_argument("--gmail-token", default=None,
                     help="OAuth 'authorized user' JSON (or $INBOX_JANITOR_GMAIL_TOKEN)")
    top.add_argument("--env-file", default=None,
                     help="KEY=VALUE file read first; real environment variables win")
    sub = top.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", parents=[common], help="one scheduled run")
    run.add_argument("--max-stage", required=True, choices=STAGES,
                     help="the autonomy ceiling; the config can only lower it")
    command = sub.add_parser("command", parents=[common], help="apply reply commands")
    command.add_argument("lines", nargs="+", metavar="COMMAND")
    sub.add_parser("status", parents=[common], help="counts only, as JSON")
    backlog = sub.add_parser("trash-backlog", parents=[common],
                             help="trash mail held back from before trashing was enabled")
    backlog.add_argument("--approve", type=int, required=True, metavar="N",
                         help="must equal the number waiting (see the digest)")
    backlog.add_argument("--max-stage", required=True, choices=STAGES)
    check = sub.add_parser("check", parents=[common], help="show the effective settings")
    check.add_argument("--max-stage", required=True, choices=STAGES)
    return top


def read_env_file(path: Path | str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def model_endpoint(env: Mapping[str, str]) -> str:
    """Where message content (and the key) go when a model classifies. That is a trust
    boundary, so it is the operator's setting in the environment, never janitor.toml; and it
    is https, unless the model runs on this machine."""
    url = env.get(LLM_URL) or DEFAULT_LLM_URL
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        parts, host = None, ""
    if parts is not None and host and (parts.scheme == "https"
                                       or (parts.scheme == "http" and host in LOOPBACK)):
        return url
    raise SetupError(f"{LLM_URL} must be an https:// URL (plain http only for a model on "
                     "this machine: localhost, 127.0.0.1 or ::1)")


def build_classifier(config: Config, env: Mapping[str, str], http: HttpFn = urllib_send):
    rules = RuleClassifier((*config.rules, *DEFAULT_RULES))
    if config.classifier.kind == "rules":
        return rules
    key = env.get(LLM_KEY, "")
    if not key:
        raise SetupError(f'set {LLM_KEY} to the model API key, or use kind = "rules"')
    llm = LLMClassifier(OpenAICompatibleChat(base_url=model_endpoint(env), api_key=key,
                                             model=config.classifier.model, http=http))
    if config.classifier.kind == "llm":
        return llm
    return FirstConfident([rules, llm], accept=config.min_confidence)


def build_mailbox(spec: str, token: str | None, now: Callable[[], datetime]) -> Mailbox:
    if spec.startswith("json:"):
        return JsonFileMailbox(spec.removeprefix("json:"), now=now)
    if spec == "gmail":
        if not token:
            raise SetupError("Gmail needs --gmail-token PATH (an OAuth 'authorized user' JSON)")
        try:
            return GmailMailbox(OAuthToken.from_file(token))
        except (OSError, ValueError) as err:
            raise SetupError(f"can't use the Gmail token file: {err}") from None
    raise SetupError("--mailbox must be gmail or json:PATH")


def describe(config: Config, pinned: Stage, env: Mapping[str, str] | None = None) -> list[str]:
    stage = effective_stage(pinned, config.stage)
    asked = f"; the config asks for {config.stage}" if config.stage is not None else ""
    classifier = config.classifier.kind
    if "llm" in classifier:                   # who would receive message content
        host = urlsplit(model_endpoint(env or {})).hostname
        classifier += f" (model {config.classifier.model} at {host}, set by {LLM_URL})"
    lines = [
        "config: OK",
        f"effective stage: {stage} (pinned by --max-stage {pinned}{asked})",
        f"confidence floor: {config.min_confidence:.2f}",
        f"trash caps: {config.trash_per_run} per run, {config.trash_per_day} per day",
        f"classifier: {classifier}",
        f"trusted senders for spam rescue: {len(config.trusted_senders)}",
        "policy: " + ", ".join(f"{c}={a.value}" for c, a in config.policy.items()),
    ]
    return lines + [f"warning: {w}" for w in config.warnings]


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None,
         now: Callable[[], datetime] | None = None) -> int:
    env = dict(os.environ) if env is None else dict(env)
    clock = now or (lambda: datetime.now(UTC))
    args = build_parser().parse_args(argv)

    def fail(message: str, code: int) -> int:
        print(f"inbox-janitor: {message}", file=sys.stderr)
        return code

    if args.env_file:
        try:
            env = {**read_env_file(args.env_file), **env}
        except OSError as err:
            return fail(f"can't read --env-file: {err.strerror}", 2)

    try:
        config = load_config(args.config)
    except ConfigError as err:
        return fail(str(err), 2)
    if args.cmd == "check":
        try:
            print("\n".join(describe(config, Stage.parse(args.max_stage), env)))
        except SetupError as err:
            return fail(str(err), 2)
        return 0
    state_dir = Path(args.state_dir or env.get("INBOX_JANITOR_STATE")
                     or "~/.inbox-janitor").expanduser()
    if args.cmd == "status":
        print(json.dumps(read_status(config, state_dir, clock), indent=2))
        return 0

    lock = RunLock(state_dir / ".lock")
    if not lock.acquire():
        return fail("another run is in progress", 75)
    try:
        mailbox = build_mailbox(args.mailbox, args.gmail_token
                                or env.get("INBOX_JANITOR_GMAIL_TOKEN"), clock)
        classifier = build_classifier(config, env) if args.cmd == "run" \
            else RuleClassifier()
        pinned = Stage.parse(getattr(args, "max_stage", "trial"))
        janitor = Janitor(mailbox=mailbox, classifier=classifier, config=config,
                          pinned_stage=pinned, state_dir=state_dir, now=clock)
        if args.cmd == "run":
            report = janitor.run()
            print(summary_line(report) + (" digest=sent" if report.digest_sent else ""))
        elif args.cmd == "command":
            for line in janitor.command("\n".join(args.lines)):
                print(line)
        elif args.cmd == "trash-backlog":
            trashed, left = janitor.trash_backlog(args.approve)
            print(f"trashed {trashed}; {left} still waiting"
                  + (f" (a trash cap was reached: approve the remaining {left} on a later run)"
                     if left else ""))
        return 0
    except SetupError as err:
        return fail(str(err), 2)
    except (BacklogMismatch, StageTooLow) as err:
        return fail(f"refused: {err}", 2)
    except WrongAccount:
        return fail("refused: the mailbox is not the configured owner's; nothing was changed",
                    3)
    except MailboxError as err:          # holds the operation and status code only
        return fail(str(err), 4)
    except Exception as err:             # never print a message: it could hold mail content
        return fail(f"failed ({type(err).__name__})", 4)
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
