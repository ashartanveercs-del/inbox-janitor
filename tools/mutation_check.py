"""Mutation check for the safety invariants: break one guard at a time and confirm that the
test written for it goes red. A guard whose test still passes when the guard is gone is not
really tested.

    python tools/mutation_check.py

Each mutation edits one source file in place, runs one test (bytecode caching off, so a
stale .pyc can never stand in for the edit), and restores the file - also on Ctrl-C.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JANITOR = "src/inbox_janitor/janitor.py"

# (file, original text, broken text, the test that must fail)
MUTATIONS = [
    (JANITOR, "self.stage = effective_stage(pinned_stage, config.stage)",
     "self.stage = max(pinned_stage, config.stage or pinned_stage)",
     "test_janitor_stages.py::test_the_config_can_lower_but_never_raise_the_pinned_stage"),
    (JANITOR, "if SENT not in m.labels or m.sender != self.config.owner \\",
     "if m.sender != self.config.owner \\",
     "test_janitor_replies.py::test_the_sent_label_is_checked_even_if_the_search_is_too_broad"),
    (JANITOR, "self._count_trash()\n            self.mailbox.trash(m.ref)",
     "self.mailbox.trash(m.ref)\n            self._count_trash()",
     "test_janitor_stages.py::test_the_trash_count_is_saved_before_the_trash_call"),
    (JANITOR, 'self.state.pop("trash_since", None)', "pass",
     "test_janitor_stages.py::test_dropping_below_trash_resets_when_trash_counts_from"),
    (JANITOR, "if self.ledger.cut_off(ref):            # a run died while working on it",
     "if False:",
     "test_janitor_robustness.py::test_a_message_that_kills_the_process_cannot_block_every_later_run"),
    (JANITOR, "if streak >= HALT_AFTER:\n                    report.halted = True",
     "if False:\n                    report.halted = True",
     "test_janitor_robustness.py::test_three_failures_in_a_row_halt_the_run"),
    (JANITOR, "if streak >= HALT_AFTER:            # an outage",
     "if False:            # an outage",
     "test_janitor_robustness.py::"
     "test_three_spam_failures_in_a_row_stop_the_spam_check_but_not_triage"),
    (JANITOR, "if self.ledger.is_final(ref) or self.ledger.decision(ref) is not None:",
     "if False:",
     "test_janitor_robustness.py::test_undoing_a_rescue_returns_it_to_spam_for_good"),
    (JANITOR, "if self.ledger.is_final(ref) or self.ledger.decision(ref) is not None:",
     "if self.ledger.is_final(ref):",
     "test_janitor_stages.py::test_mail_the_owner_puts_back_in_spam_is_never_rescued_again"),
    (JANITOR, '-{secrets.token_hex(3)}"', '"',
     "test_janitor_replies.py::test_undo_last_only_undoes_the_last_run_even_within_the_same_second"),
    (JANITOR, "            if stored and stored != message_id:\n                ids.append(stored)",
     "            pass",
     "test_janitor_replies.py::"
     "test_replies_still_match_when_the_provider_rewrites_the_digest_message_id"),
    (JANITOR, "if len(rows) != approve:", "if False:",
     "test_janitor_stages.py::test_mail_from_before_trash_was_enabled_waits_for_an_exact_approval"),
    ("src/inbox_janitor/policy.py", "if c.confidence < config.min_confidence:",
     "if c.confidence < 0.5:",
     "test_janitor_stages.py::test_below_the_floor_nothing_happens_and_it_is_reported"),
    ("src/inbox_janitor/policy.py", "if STARRED in m.labels:", "if False:",
     "test_janitor_stages.py::test_exempt_mail_is_never_classified_or_touched"),
    ("src/inbox_janitor/rescue.py", "if found_id != serv_id.lower():", "if False:",
     "test_rescue.py::test_a_verdict_from_some_other_server_is_ignored"),
    (JANITOR, '"fp": {"sender": fps.sender, "msgid": fps.msgid, "content": fps.content}}',
     '"fp": {"sender": fps.sender, "msgid": fps.msgid, "content": fps.content}, '
     '"subject": m.subject}',
     "test_janitor_robustness.py::test_nothing_on_disk_holds_a_subject_an_address_or_a_body"),
    ("src/inbox_janitor/commands.py", 'match = _QUOTE_START.search(body or "")', "match = None",
     "test_janitor_replies.py::test_a_reply_cannot_raise_the_stage_or_lower_the_floor"),
    ("src/inbox_janitor/classify/__init__.py",
     "if isinstance(confidence, bool) or not isinstance(confidence, int | float | str):",
     "if not isinstance(confidence, int | float | str):",
     "test_classify_rules.py::test_checked_turns_a_bad_confidence_into_an_invalid_answer"),
    ("src/inbox_janitor/mailbox/gmail.py", "except (LookupError, ValueError):",
     "except LookupError:",
     "test_gmail.py::test_a_charset_that_cannot_decode_falls_back_to_utf8"),
    (JANITOR, "self._rescue_one(ref, report)\n                streak = 0\n"
              "            except Exception as err:",
     "self._rescue_one(ref, report)\n                streak = 0\n"
     "            except MailboxError as err:",
     "test_janitor_robustness.py::test_one_spam_message_that_fails_to_parse_does_not_stop_triage"),
    (JANITOR, "        if died_on:", "        if False:",
     "test_janitor_robustness.py::"
     "test_a_spam_message_that_kills_the_process_cannot_block_every_later_run"),
    ("src/inbox_janitor/config.py", "elif is_system_label(name):", "elif False:",
     "test_config.py::test_a_label_can_never_be_a_system_label"),
    (JANITOR, "if any(is_system_label(lb) and lb != INBOX for lb in adding):", "if False:",
     "test_janitor_stages.py::"
     "test_filing_never_adds_trash_or_spam_even_from_a_config_built_around_the_parser"),
    (JANITOR, "return line if after is None else max(line, after)",
     "return line if after is None else after",
     "test_janitor_stages.py::test_the_config_cannot_move_the_backlog_line_earlier"),
    (JANITOR, "        if not retry:", "        if True:",
     "test_janitor_replies.py::test_a_reply_that_hit_a_mailbox_error_is_applied_on_a_later_run"),
]


def main() -> int:
    survivors = []
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    for rel, old, new, test in MUTATIONS:
        path = ROOT / rel
        original = path.read_bytes()            # bytes: restore exactly, line endings included
        text = original.decode("utf-8")
        if old not in text:
            print(f"MISSING   {rel}: {old[:60]!r}")
            survivors.append(test)
            continue
        try:
            path.write_bytes(text.replace(old, new, 1).encode("utf-8"))
            for cache in (ROOT / "src").rglob("__pycache__"):
                shutil.rmtree(cache, ignore_errors=True)
            result = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-x",
                                     f"tests/{test}"], cwd=ROOT, env=env, capture_output=True,
                                    text=True, check=False)
        finally:
            path.write_bytes(original)
        killed = result.returncode != 0
        print(("killed    " if killed else "SURVIVED  ") + test.split("::")[1])
        if not killed:
            survivors.append(test)
    print(f"\n{len(MUTATIONS) - len(survivors)}/{len(MUTATIONS)} mutations caught by their test")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
