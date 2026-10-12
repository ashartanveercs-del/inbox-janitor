"""Cheap and certain first: try classifiers in order, stop at the first confident answer."""

from __future__ import annotations

from collections.abc import Sequence

from ..models import Classification, Message
from . import Classifier


class FirstConfident:
    name = "chain"

    def __init__(self, classifiers: Sequence[Classifier], accept: float = 0.95) -> None:
        if not classifiers:
            raise ValueError("FirstConfident needs at least one classifier")
        self.classifiers = tuple(classifiers)
        self.accept = accept

    def classify(self, message: Message) -> Classification:
        answers = []
        for classifier in self.classifiers:
            got = classifier.classify(message)
            if got.error is None and got.confidence >= self.accept:
                return got
            answers.append(got)
        # an unsure message whose better-informed classifier was down waits for it
        down = [a for a in answers if a.error == "unavailable"]
        if down:
            return down[0]
        valid = [a for a in answers if a.error is None]
        if valid:
            return max(valid, key=lambda a: a.confidence)
        return answers[-1]
