"""The examples are part of the contract: they run offline and tell the true story."""

import runpy
import sys
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def run_example(name: str, capsys) -> str:
    sys.path.insert(0, str(EXAMPLES))
    try:
        runpy.run_path(str(EXAMPLES / name), run_name="__main__")
    finally:
        sys.path.remove(str(EXAMPLES))
    return capsys.readouterr().out


def test_the_demo_walks_trial_file_reply_trash_and_backlog(capsys):
    out = run_example("demo.py", capsys)
    assert "stage=trial" in out and "rescued=1" in out and "suggested=9" in out
    assert "Inbox janitor (file)" in out and "7 filed" in out
    assert "undo #3: put back" in out and "rule saved: never touch that sender's mail" in out
    assert "stage=trash backlog=2 done=1 trashed=1" in out and "--approve 2" in out
    assert "trashed 2; 0 still waiting" in out
    assert "failed authentication and stayed in Spam" in out      # the bank-details lure


def test_the_custom_classifier_example_shows_who_decided(capsys):
    out = run_example("custom_classifier.py", capsys)
    assert "(decided by catering): kept" in out
    assert "(decided by llm): suggested trash" in out
    assert "(decided by rules): done archive" in out
