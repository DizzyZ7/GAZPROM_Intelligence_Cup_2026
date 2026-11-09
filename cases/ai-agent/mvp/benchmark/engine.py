"""Сравнимые методы: одинаковая классификация, независимые сходство и кластеры."""

import hashlib
import json
import time
import tracemalloc
from itertools import combinations
from math import isfinite

from pulse import Ticket, classify, redact
from semantic import cosine
from ticket_context import ANALYSIS_VERSION, classify_context

from .tfidf import sparse_cosine

METHODS = ("rules", "tfidf", "embeddings")
CLASSIFIER_VERSIONS = {"rules": "rules-v1", "context": ANALYSIS_VERSION}
CLASSIFIERS = tuple(CLASSIFIER_VERSIONS)
MAX_PAIRS = 200000


def _attach_groups(predictions, pairs):
    matches = {(pair["left"], pair["right"]): pair["matched"] for pair in pairs}
    groups = []
    for row in predictions:
        identity = row["ticket_id"]
        for group in groups:
            if all(matches[tuple(sorted((identity, other)))] for other in group):
                group.append(identity)
                break
        else:
            groups.append([identity])
    membership = {
        identity: hashlib.sha256(
            json.dumps(group, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()[:20]
        for group in groups
        for identity in group
    }
    for row in predictions:
        row["cluster_id"] = membership[row["ticket_id"]]
    for pair in pairs:
        pair["clustered"] = membership[pair["left"]] == membership[pair["right"]]
    return groups


def apply_threshold(analysis, threshold):
    if (
        analysis["method"] == "rules"
        or isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not isfinite(threshold)
        or not 0 < threshold <= 1
    ):
        raise ValueError("Неверный порог поиска")
    predictions = [dict(row) for row in analysis["predictions"]]
    pairs = [dict(pair) for pair in analysis["pairs"]]
    for pair in pairs:
        pair["matched"] = pair["score"] is not None and pair["score"] >= threshold
        if pair["score"] is not None:
            pair["reason"] = "similarity_pass" if pair["matched"] else "below_threshold"
    groups = _attach_groups(predictions, pairs)
    return dict(
        analysis,
        threshold=threshold,
        predictions=predictions,
        pairs=pairs,
        groups=groups,
        performance=None,
    )


def _context(left, right):
    if left["product"] != right["product"]:
        return "different_product"
    if left["category"] != right["category"]:
        return "different_category"
    if (
        left["category"] == "unknown"
        or not left["workflow"]
        or left["workflow"] == "unclassified"
    ):
        return "classification_abstained"
    if left["workflow"] != right["workflow"]:
        return "different_workflow"
    if left.get("groupable") is False or right.get("groupable") is False:
        return "classification_abstained"
    for field in ("operation", "symptom"):
        if left.get(field) != right.get(field):
            return "different_" + field
    return "context_pass"


def _validated_vectors(provider, texts):
    vectors = provider.embed(texts)
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise ValueError("Неверное число embedding-векторов")
    dims = set()
    for vector in vectors:
        if (
            not isinstance(vector, list)
            or not vector
            or any(
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not isfinite(v)
                for v in vector
            )
        ):
            raise ValueError("Неверное значение embedding-вектора")
        cosine(vector, vector)  # Проверка нулевой нормы и диапазона чисел.
        dims.add(len(vector))
    if len(dims) > 1:
        raise ValueError("Разная размерность embedding-векторов")
    return vectors


def analyze_method(
    method,
    tickets,
    threshold=None,
    tfidf=None,
    provider=None,
    max_pairs=MAX_PAIRS,
    *,
    classifier="rules",
):
    if (
        method not in METHODS
        or classifier not in CLASSIFIERS
        or len({t.id for t in tickets}) != len(tickets)
    ):
        raise ValueError("Неизвестный метод или повторные ID")
    if (
        type(max_pairs) is not int
        or max_pairs < 0
        or len(tickets) * (len(tickets) - 1) // 2 > max_pairs
    ):
        raise ValueError("Превышен лимит числа пар benchmark")
    if method != "rules" and (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not isfinite(threshold)
        or not 0 < threshold <= 1
    ):
        raise ValueError("Порог должен находиться в (0,1]")
    if (method == "tfidf" and tfidf is None) or (
        method == "embeddings" and provider is None
    ):
        raise ValueError("Метод требует явного локального провайдера признаков")
    own_trace = not tracemalloc.is_tracing()
    if own_trace:
        tracemalloc.start()
    started, cpu_start = time.perf_counter(), time.process_time()
    try:
        sanitized = [
            Ticket(t.id, t.product, redact(t.text), t.channel)
            for t in sorted(tickets, key=lambda t: t.id)
        ]
        predictions = []
        for ticket in sanitized:
            category, workflow, score = classify(ticket.text)
            row = {
                "ticket_id": ticket.id,
                "product": ticket.product,
                "category": category,
                "workflow": workflow,
                "classifier_score": score,
            }
            if classifier == "context":
                context = classify_context(ticket.text)
                row.update(
                    category=context.category,
                    workflow=context.workflow,
                    classifier_score=None,
                    operation=context.operation,
                    symptom=context.symptom,
                    reason=context.reason,
                    groupable=context.groupable,
                )
            predictions.append(row)
        classified = time.perf_counter()
        texts = [t.text for t in sanitized]
        eligible = [
            i for i, row in enumerate(predictions) if row.get("groupable", True)
        ]
        vectors = None
        if method in {"tfidf", "embeddings"}:
            selected = [texts[i] for i in eligible]
            represented_vectors = (
                tfidf.transform(selected)
                if method == "tfidf"
                else (_validated_vectors(provider, selected) if selected else [])
            )
            vectors = dict(zip(eligible, represented_vectors))
        represented = time.perf_counter()
        pairs = []
        for i, j in combinations(range(len(predictions)), 2):
            left, right = predictions[i], predictions[j]
            reason = _context(left, right)
            score = None
            if reason == "context_pass":
                score = (
                    1.0
                    if method == "rules"
                    else (
                        sparse_cosine(vectors[i], vectors[j])
                        if method == "tfidf"
                        else max(-1.0, min(1.0, cosine(vectors[i], vectors[j])))
                    )
                )
            matched = reason == "context_pass" and (
                method == "rules" or score >= threshold
            )
            if reason == "context_pass":
                reason = (
                    "rules_match"
                    if method == "rules"
                    else ("similarity_pass" if matched else "below_threshold")
                )
            pairs.append(
                {
                    "left": left["ticket_id"],
                    "right": right["ticket_id"],
                    "score": score,
                    "matched": matched,
                    "reason": reason,
                }
            )
        scored = time.perf_counter()
        groups = _attach_groups(predictions, pairs)
        ended = time.perf_counter()
        peak = tracemalloc.get_traced_memory()[1] if own_trace else None
        performance = {
            "total_seconds": ended - started,
            "cpu_seconds": time.process_time() - cpu_start,
            "classification_seconds": classified - started,
            "representation_seconds": represented - classified,
            "pair_scoring_seconds": scored - represented,
            "clustering_seconds": ended - scored,
            "tickets_per_second": len(tickets) / (ended - started)
            if ended > started
            else None,
            "python_peak_bytes": peak,
            "ticket_count": len(tickets),
            "pair_count": len(pairs),
        }
        return {
            "method": method,
            "classifier": (
                "rules-v1-shared; score не является вероятностью"
                if classifier == "rules"
                else ANALYSIS_VERSION
            ),
            "threshold": threshold,
            "predictions": predictions,
            "pairs": pairs,
            "groups": groups,
            "performance": performance,
        }
    finally:
        if own_trace:
            tracemalloc.stop()
