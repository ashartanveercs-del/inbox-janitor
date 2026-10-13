# Design claims and their evidence

[README](../README.md) · [Executed verification](VERIFICATION.md)

The classifier proposes a category and confidence. Deterministic policy, the operator's
stage ceiling and persisted safety state decide whether any mailbox operation is allowed.

| Claim | Implementation | Evidence |
|---|---|---|
| Editable configuration cannot raise the operator's stage or lower the floor | [stages.py](../src/inbox_janitor/stages.py), [config.py](../src/inbox_janitor/config.py), [policy.py](../src/inbox_janitor/policy.py) | [test_janitor_stages.py](../tests/test_janitor_stages.py), [test_config.py](../tests/test_config.py): caps, invalid confidence, forbidden system labels and a config built around the parser |
| Interrupted changes remain undoable | [janitor.py](../src/inbox_janitor/janitor.py), [storage.py](../src/inbox_janitor/storage.py), [commands.py](../src/inbox_janitor/commands.py) | [test_janitor_robustness.py](../tests/test_janitor_robustness.py): process death between provider mutation and decision, `undo last`, resumed operations |
| Digest subjects and quoted mail cannot become owner commands | [digest.py](../src/inbox_janitor/digest.py), [commands.py](../src/inbox_janitor/commands.py) | [test_digest.py](../tests/test_digest.py), [test_janitor_replies.py](../tests/test_janitor_replies.py): hostile subjects, quoted commands and a search that leaks non-SENT messages |
| Spoofed trusted senders are not rescued from spam | [rescue.py](../src/inbox_janitor/rescue.py) | [test_rescue.py](../tests/test_rescue.py): mailbox-server provenance, DMARC, aligned DKIM and injected authentication headers |
| Local message records omit raw subjects, addresses and bodies | [privacy.py](../src/inbox_janitor/privacy.py), [userrules.py](../src/inbox_janitor/userrules.py) | [test_privacy.py](../tests/test_privacy.py), [privacy canary](../tests/test_janitor_robustness.py): all janitor-written files searched after a multi-stage workflow |
| Reported confidence is measured, not treated as calibration | [evaluation.py](../src/inbox_janitor/evaluation.py), [corpus.json](../eval/corpus.json) | [test_evaluation.py](../tests/test_evaluation.py): README report matches the 108-message synthetic corpus; Wilson bounds and incorrect actions are reported |
| Every selected mutation needs a real failing test | [mutation_check.py](../tools/mutation_check.py) | 38 mutations; [test_mutation_check.py](../tests/test_mutation_check.py) requires a clean baseline, rejects collection/usage errors and checks byte-for-byte restoration |

## Boundaries

- Gmail behavior is tested against scripted HTTP responses, not a live Gmail account. OAuth
  scopes, provider behavior and account configuration still need verification for deployment.
- The operator must protect the scheduler command, environment and state directory from the
  agent. The stage flag is not an OS access-control mechanism by itself.
- Trial is suggestion-only for inbox triage; authenticated trusted-sender spam rescue is an
  explicit exception. A classifier's `0.95` confidence is not a measured 95% probability.
- The synthetic corpus was developed alongside the rules. Its 51/57 precision-at-floor
  result and zero false trash describe this regression fixture, not unseen inbox accuracy.
- LLM classification sends bounded message content to the configured provider. Use the rules
  classifier for an entirely local workflow. Keyed fingerprints protect shared records only
  while the fingerprint key is kept separate from anyone inspecting those records.
- Undo is limited by provider message retention and the janitor's ledger retention; it cannot
  restore mail permanently removed outside the tool. No permanent-delete method is exposed.

The complete configuration and operational assumptions remain in the README's
[security notes](../README.md#security-notes).
