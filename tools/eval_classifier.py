"""Measure a classifier on labelled mail: precision at the confidence floor, per category.

    python tools/eval_classifier.py                          # the built-in rules, offline
    python tools/eval_classifier.py --corpus my-labels.json  # your own labelled export
    python tools/eval_classifier.py --classifier llm --model NAME
    python tools/eval_classifier.py --classifier rules+llm --model NAME

The model's endpoint and key come from INBOX_JANITOR_LLM_URL and INBOX_JANITOR_LLM_KEY, exactly
as for the janitor itself. A corpus is the demo mailbox's JSON (examples/mailbox.json) with a
"label" on every message; eval/corpus.json is the bundled one. The report names messages by
their ref only, never by subject or address.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from inbox_janitor.cli import SetupError, build_classifier
from inbox_janitor.config import Config, ConfigError
from inbox_janitor.evaluation import evaluate, load_corpus, render

CORPUS = Path(__file__).resolve().parent.parent / "eval" / "corpus.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--corpus", default=str(CORPUS), help="labelled mail (JSON)")
    parser.add_argument("--classifier", choices=("rules", "llm", "rules+llm"), default="rules")
    parser.add_argument("--model", default="", help="the model name, for llm and rules+llm")
    args = parser.parse_args(argv)
    owner, corpus = load_corpus(args.corpus)
    settings = {"kind": args.classifier, **({"model": args.model} if args.model else {})}
    try:
        config = Config.from_dict({"owner": owner, "classifier": settings})
        classifier = build_classifier(config, os.environ)
    except (ConfigError, SetupError) as err:
        print(f"eval_classifier: {err}", file=sys.stderr)
        return 2
    print(render(evaluate(classifier, corpus, floor=config.min_confidence)), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
