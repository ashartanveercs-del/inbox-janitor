"""The evaluation: the numbers in the README are the numbers the rules get, and the safety
claims made from them hold on the bundled corpus."""

import json
import re
import runpy
import sys
from pathlib import Path

import pytest
from factories import msg
from fakes import ScriptedClassifier

from inbox_janitor.addresses import domain_of
from inbox_janitor.classify import CATEGORIES
from inbox_janitor.classify.rules import RuleClassifier
from inbox_janitor.evaluation import Labelled, evaluate, load_corpus, render, wilson_lower
from inbox_janitor.models import Classification

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "eval" / "corpus.json"


def rules_on_the_corpus():
    owner, corpus = load_corpus(CORPUS)
    return evaluate(RuleClassifier(own_domains=(domain_of(owner),)), corpus)


def test_the_readme_reports_exactly_what_the_rules_measure():
    """Change the rules or the corpus and this fails until the README says so - the table,
    and the numbers its prose quotes."""
    ev = rules_on_the_corpus()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    block = re.search(r"<!-- eval:rules -->\n(.*?)<!-- /eval:rules -->", readme, re.S)
    assert block is not None and block.group(1) == render(ev)
    prose = " ".join(readme.split())
    assert f"is {ev.total} made-up messages" in prose
    stated, right = ev.bins[4]                         # stated 0.95-0.97
    assert f"0.95-0.97 are right {round(100 * right / stated)}% of the time" in prose


def test_on_the_corpus_the_rules_trash_nothing_by_mistake_and_move_no_kept_mail():
    ev = rules_on_the_corpus()
    assert ev.trashed_by_mistake == [] and ev.kept_mail_moved == []


def test_the_corpus_covers_every_category_with_made_up_addresses_only():
    owner, corpus = load_corpus(CORPUS)
    assert {item.label for item in corpus} == set(CATEGORIES)
    for address in [owner, *(item.message.sender for item in corpus)]:
        assert domain_of(address) == "example.com" or domain_of(address).endswith(
            ".example.com"), address


def test_a_corpus_with_an_unknown_label_or_a_repeated_ref_is_refused(tmp_path):
    path = tmp_path / "labels.json"
    entry = {"ref": "a", "from": "x@shop.example.com", "subject": "Hi", "label": "invoice"}
    path.write_text(json.dumps({"owner": "o@example.com", "messages": [
        {**entry, "label": "spam"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="label"):
        load_corpus(path)
    path.write_text(json.dumps({"owner": "o@example.com", "messages": [entry, entry]}),
                    encoding="utf-8")
    with pytest.raises(ValueError, match="unique"):
        load_corpus(path)


@pytest.mark.parametrize(("right", "n", "low"), [(15, 15, 0.796), (9, 9, 0.701),
                                                 (42, 48, 0.753), (0, 5, 0.0)])
def test_the_wilson_lower_bound(right, n, low):
    assert wilson_lower(right, n) == pytest.approx(low, abs=0.001)
    assert wilson_lower(0, 0) == 0.0


def labelled(ref: str, label: str) -> Labelled:
    return Labelled(ref, msg(subject=ref), label)


def test_evaluate_scores_answers_at_the_floor_and_names_the_dangerous_mistakes():
    answers = {"a": Classification("promotion", 0.99, "x"),     # right, trashed
               "b": Classification("promotion", 0.98, "x"),     # a newsletter, trashed
               "c": Classification("newsletter", 0.96, "x"),    # personal mail, archived
               "d": Classification("contract", 0.95, "x"),      # personal mail, labelled
               "e": Classification("invoice", 0.90, "x"),       # below the floor
               "f": Classification.invalid("x")}
    corpus = [labelled("a", "promotion"), labelled("b", "newsletter"),
              labelled("c", "personal"), labelled("d", "personal"),
              labelled("e", "invoice"), labelled("f", "receipt")]
    ev = evaluate(ScriptedClassifier(answers), corpus)
    promo, invoice = ev.scores["promotion"], ev.scores["invoice"]
    assert (promo.labelled, promo.answered, promo.right) == (1, 2, 1)
    assert promo.precision == 0.5 and promo.recall == 1.0
    assert (invoice.answered, invoice.precision, invoice.recall) == (0, None, 0.0)
    assert ev.trashed_by_mistake == ["b"] and ev.kept_mail_moved == ["c"]
    assert [w[0] for w in ev.wrong] == ["b", "c", "d"]
    assert (ev.total, ev.unusable, ev.acted) == (6, 1, 4)
    assert ev.bins[3] == [1, 1]                       # 0.90-0.95: the invoice, right


def run_tool(*args):
    old = sys.argv
    sys.argv = ["eval_classifier.py", *args]
    try:
        with pytest.raises(SystemExit) as done:
            runpy.run_path(str(ROOT / "tools" / "eval_classifier.py"), run_name="__main__")
    finally:
        sys.argv = old
    return done.value.code


def test_the_tool_prints_the_report(capsys):
    assert run_tool() == 0
    out = capsys.readouterr().out
    assert out == render(rules_on_the_corpus())
    assert "@" not in out                             # message refs only, never addresses


def test_the_tool_needs_a_key_to_measure_a_model(capsys, monkeypatch):
    monkeypatch.delenv("INBOX_JANITOR_LLM_KEY", raising=False)
    assert run_tool("--classifier", "llm", "--model", "small-model") == 2
    assert "INBOX_JANITOR_LLM_KEY" in capsys.readouterr().err
