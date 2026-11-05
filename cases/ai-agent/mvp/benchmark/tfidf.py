"""Разреженный смешанный TF-IDF без внешних зависимостей и обучающих меток."""

import re
from collections import Counter
from math import isfinite, log, sqrt


def _terms(text):
    words = re.findall(r"\w+", text.casefold(), flags=re.UNICODE)
    result = Counter("w1:" + word for word in words)
    result.update("w2:" + a + " " + b for a, b in zip(words, words[1:]))
    for word in words:
        padded = " " + word + " "
        for n in (3, 4, 5):
            result.update(
                f"c{n}:" + padded[i : i + n] for i in range(len(padded) - n + 1)
            )
    return result


class TfidfModel:
    def __init__(self, idf, document_count):
        self.idf = dict(idf)
        self.document_count = document_count

    @classmethod
    def fit(cls, texts):
        if not texts or any(not isinstance(text, str) for text in texts):
            raise ValueError("Нужны development-документы без эталонных меток")
        frequencies = Counter()
        for text in texts:
            frequencies.update(_terms(text).keys())
        return cls(
            {
                term: 1 + log((1 + len(texts)) / (1 + df))
                for term, df in sorted(frequencies.items())
            },
            len(texts),
        )

    def transform(self, texts):
        vectors = []
        for text in texts:
            weighted = {
                term: (1 + log(tf)) * self.idf[term]
                for term, tf in _terms(text).items()
                if term in self.idf
            }
            norms = {
                branch: sqrt(
                    sum(v * v for t, v in weighted.items() if t.startswith(branch))
                )
                for branch in ("w", "c")
            }
            merged = {term: value / norms[term[0]] for term, value in weighted.items()}
            norm = sqrt(sum(v * v for v in merged.values()))
            vectors.append(
                {term: value / norm for term, value in sorted(merged.items())}
                if norm
                else {}
            )
        return vectors

    def to_state(self):
        return {
            "version": 1,
            "document_count": self.document_count,
            "idf": dict(sorted(self.idf.items())),
        }

    @classmethod
    def from_state(cls, state):
        if (
            set(state) != {"version", "document_count", "idf"}
            or state["version"] != 1
            or type(state["document_count"]) is not int
            or state["document_count"] < 1
        ):
            raise ValueError("Неверное состояние TF-IDF")
        if not isinstance(state["idf"], dict) or any(
            not isinstance(term, str)
            or not re.match(r"^(w[12]|c[345]):.+", term)
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            or not 1 <= value <= 1 + log((1 + state["document_count"]) / 2)
            for term, value in state["idf"].items()
        ):
            raise ValueError("Неверные веса TF-IDF")
        return cls(state["idf"], state["document_count"])


def sparse_cosine(left, right):
    if len(left) > len(right):
        left, right = right, left
    return max(0.0, min(1.0, sum(v * right.get(t, 0.0) for t, v in left.items())))
