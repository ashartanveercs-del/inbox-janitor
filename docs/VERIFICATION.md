# Verification record — 2026-10-12

[README](../README.md) · [Design proof map](PROOF_MAP.md) · [CI workflow](../.github/workflows/ci.yml)

Executed locally on Windows 11 with CPython 3.12.10, from the repository root, using the
existing development virtual environment. The Gmail adapter used scripted HTTP; the demo
used a temporary JSON mailbox. No live mailbox, API key, model call or email send was used.
The date is the review's local date (America/Los_Angeles).

| Exact PowerShell command | Observed result |
|---|---|
| `.\.venv\Scripts\python.exe -m pytest -q -ra` | **443 passed, 2 skipped**, 0 failed; 5.55 s |
| `.\.venv\Scripts\python.exe -m ruff check src tests examples tools` | All checks passed |
| `.\.venv\Scripts\python.exe examples\demo.py` | Exit 0; trial suggestions and authenticated spam rescue, filing, owner undo/rule reply, fresh trash, exact-count backlog approval, counts-only status |
| `.\.venv\Scripts\python.exe tools\mutation_check.py` | Exit 0; clean baseline passed; **38/38 mutations caught by their designated failing tests** |
| `.\.venv\Scripts\python.exe tools\eval_classifier.py` | Exit 0; 108 synthetic messages, 57 answers at floor 0.95, 51 correct (precision 0.89, 95% Wilson lower bound 0.79), no false trash, no personal/other mail moved out of inbox |

The two skips are `tests/test_privacy.py:51` and `tests/test_storage.py:117`: POSIX mode-bit
checks are unavailable on Windows. CI runs Linux/Python 3.11, 3.12 and 3.13 with lint, the
full suite, the real-CLI demo, the mutation check and the synthetic-corpus evaluation.

## Fix validated in this review

The mutation harness previously credited every nonzero pytest status as a caught guard.
New regressions reproduced six failing cases before the fix; all seven now pass. The
harness requires a green baseline and the designated test's `FAILED` summary with pytest's
test-failure exit status. Collection/usage/no-tests errors fail the check. It removes only
the mutated module's bytecode cache and restores source bytes even when pytest times out.

## What this proves and what remains outside it

The suite exercises stage ceilings, floors, caps, backlog approvals, owner command grammar,
spoof-resistant spam rescue, crash-aware undo, duplicate semantics, privacy canaries and
Gmail protocol behavior against scripted responses. The 38 mutations demonstrate that
selected guards affect behavior. The corpus was developed alongside the rules, so its
results are a regression measurement rather than unseen inbox accuracy or confidence
calibration. No live provider or external LLM deployment was validated. See [the boundaries](PROOF_MAP.md#boundaries).

## Publication audit

The pre-publication audit inspected 65 tracked files and every reachable historical blob:
10 commits, 128 unique blobs. It checked file inventory and commit subjects, known credential
formats/private-key markers, private fleet names and paths, embedded addresses and URL
hosts. No credential or private infrastructure candidates were found. Mail is fictional,
using `example.com` and reserved `.invalid` Message-IDs; non-example URLs are public Google/
OpenAI endpoints and hostile test fixtures. `.env.example` contains no token or key values.
Public author attribution is intentional. No publication blocker was found; no history was
rewritten.

## Python 3.11 compatibility follow-up

The first hosted CI run exposed a collection failure on Python 3.11: `MappingProxyType`
defaults are unhashable there, and dataclasses refuse them as field defaults. Python 3.12
hashes that type, so the initial local run did not expose the difference. The corrected
configuration uses factories that create independent, read-only policy and label maps.

Two new configuration regressions failed before the change and now pass. The original
`Config` declaration was also reproduced under an official checksum-verified Python 3.11.9
interpreter, where it raised the same `ValueError` as hosted CI. Final complete local runs
on Python 3.11.9 and 3.12.10 each passed **443 tests with 2 POSIX permissions skips**.
Ruff passed. The fix preserves immutable mappings and changes no permission ceiling.

The corrected mutation gate also passed **38/38 under both Python 3.11.9 and 3.12.10**.
