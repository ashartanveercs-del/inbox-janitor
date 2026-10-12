import json
import os
import stat

import pytest

from inbox_janitor.storage import Ledger, State

DAY = 86400.0


class Clock:
    def __init__(self, t: float = 1_760_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def decision(ref="m1", ticket=1, **kw):
    row = {"kind": "decision", "ref": ref, "ticket": ticket, "outcome": "done", "applied": []}
    row.update(kw)
    return row


def test_rows_survive_a_reload(tmp_path):
    path, clock = tmp_path / "ledger.jsonl", Clock()
    Ledger(path, now=clock).append(decision(category="newsletter"))
    again = Ledger(path, now=clock)
    assert again.decision("m1")["category"] == "newsletter"
    assert again.decision("m1")["ts"] == clock.t


def test_a_torn_last_line_is_dropped_and_the_file_repaired(tmp_path):
    path, clock = tmp_path / "ledger.jsonl", Clock()
    ledger = Ledger(path, now=clock)
    ledger.append(decision("m1", 1))
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "decision", "ref": "m2", "tick')      # the run died mid-write
    repaired = Ledger(path, now=clock)
    assert repaired.decision("m1") is not None and repaired.decision("m2") is None
    repaired.append(decision("m3", 3))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["ref"] for line in lines] == ["m1", "m3"]


def test_rows_older_than_the_retention_window_are_dropped(tmp_path):
    path, clock = tmp_path / "ledger.jsonl", Clock()
    Ledger(path, now=clock).append(decision("old", 1))
    clock.t += 200 * DAY
    Ledger(path, now=clock, keep_days=180).append(decision("new", 2))
    assert [json.loads(x)["ref"] for x in path.read_text().splitlines()] == ["new"]


def test_tickets_map_to_refs_and_are_never_reused(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", now=Clock())
    ledger.append(decision("m1", 4))
    ledger.append(decision("m2", 9))
    assert ledger.ref_of(9) == "m2" and ledger.ticket_of("m1") == 4
    assert ledger.max_ticket() == 9


def test_a_start_with_nothing_after_it_means_a_run_died_on_that_message(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", now=Clock())
    ledger.append({"kind": "start", "ref": "m1", "ticket": 1})
    assert ledger.cut_off("m1")
    ledger.append({"kind": "error", "ref": "m1", "ticket": 1, "what": "Killed"})
    assert not ledger.cut_off("m1") and ledger.failures("m1") == 1
    ledger.append({"kind": "start", "ref": "m1", "ticket": 1})
    ledger.append(decision("m1", 1))
    assert not ledger.cut_off("m1") and ledger.failures("m1") == 0


def test_applied_changes_accumulate_until_undone(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", now=Clock())
    ledger.append(decision("m1", 1, applied=[["unlabel", "SPAM"], ["label", "INBOX"]]))
    ledger.append(decision("m1", 1, applied=[["label", "Janitor/Receipts"],
                                             ["unlabel", "INBOX"]]))
    assert ledger.history("m1") == [["unlabel", "SPAM"], ["label", "INBOX"],
                                    ["label", "Janitor/Receipts"], ["unlabel", "INBOX"]]
    ledger.append({"kind": "undo", "ticket": 1, "ok": True})
    assert ledger.history("m1") == [] and ledger.is_final("m1") and ledger.undone("m1")


def test_keep_makes_a_message_final_without_undoing_it(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", now=Clock())
    ledger.append(decision("m1", 1, outcome="suggested"))
    ledger.append({"kind": "keep", "ticket": 1})
    assert ledger.is_final("m1") and not ledger.undone("m1")


def test_processed_replies_are_remembered_by_fingerprint(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", now=Clock())
    ledger.append({"kind": "reply", "fp": "abc123"})
    assert Ledger(tmp_path / "l.jsonl", now=Clock()).seen_reply("abc123")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_the_ledger_is_private(tmp_path):
    path = tmp_path / "l.jsonl"
    Ledger(path, now=Clock()).append(decision())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_state_round_trips(tmp_path):
    state = State(tmp_path / "state.json")
    assert dict(state) == {} and not state.was_reset
    state["next_ticket"] = 5
    state.save()
    assert State(tmp_path / "state.json")["next_ticket"] == 5
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]   # no temp file left behind


@pytest.mark.parametrize("content", ["{not json", "[1, 2, 3]", ""])
def test_a_damaged_state_file_is_set_aside_and_started_fresh(tmp_path, content):
    path = tmp_path / "state.json"
    path.write_text(content, encoding="utf-8")
    state = State(path)
    assert dict(state) == {} and state.was_reset
    assert any(p.name.startswith("state.json.bad-") for p in tmp_path.iterdir())
