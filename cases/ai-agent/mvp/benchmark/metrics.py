"""Метрики отдельных задач; эталоны используются только при оценке результата."""

from collections import Counter

CATEGORIES = ("bug", "feature_request", "how_to", "service_request", "other", "unknown")


def pair_metrics(gold, predicted):
    if len(gold) != len(predicted) or any(
        type(x) is not bool for x in gold + predicted
    ):
        raise ValueError("Ожидаются два равных списка логических меток")
    tp = sum(g and p for g, p in zip(gold, predicted))
    fp = sum(not g and p for g, p in zip(gold, predicted))
    fn = sum(g and not p for g, p in zip(gold, predicted))
    tn = len(gold) - tp - fp - fn
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {
        "pair_count": len(gold),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": precision,
        "recall": recall,
        "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0.0,
        "false_merge_fraction": fp / (tp + fp) if tp + fp else None,
        "missed_repeat_fraction": fn / (tp + fn) if tp + fn else None,
    }


def classification_metrics(gold, predicted):
    if len(gold) != len(predicted) or any(
        x not in CATEGORIES for x in gold + predicted
    ):
        raise ValueError("Неверные категории или разная длина разметки")
    counts = Counter(zip(gold, predicted))
    matrix = {g: {p: counts[g, p] for p in CATEGORIES} for g in CATEGORIES}
    per_category = {}
    for category in CATEGORIES:
        binary = pair_metrics(
            [g == category for g in gold], [p == category for p in predicted]
        )
        per_category[category] = {k: binary[k] for k in ("precision", "recall", "f1")}
        per_category[category]["support"] = sum(g == category for g in gold)
    return {
        "count": len(gold),
        "confusion_matrix": matrix,
        "per_category": per_category,
        "accuracy": sum(g == p for g, p in zip(gold, predicted)) / len(gold)
        if gold
        else 0.0,
        "macro_precision": sum(x["precision"] for x in per_category.values())
        / len(CATEGORIES),
        "macro_recall": sum(x["recall"] for x in per_category.values())
        / len(CATEGORIES),
        "macro_f1": sum(x["f1"] for x in per_category.values()) / len(CATEGORIES),
        "abstention_fraction": predicted.count("unknown") / len(predicted)
        if predicted
        else 0.0,
    }


def retrieval_metrics(gold_clusters, ranked, k=5):
    if type(k) is not int or k < 1 or set(ranked) != set(gold_clusters):
        raise ValueError("Неверная конфигурация top-k")
    relevant = Counter(c for c in gold_clusters.values() if c is not None)
    returned = hits = possible = queries = query_hits = 0
    for identity, neighbors in ranked.items():
        selected = neighbors[:k]
        if (
            len(set(selected)) != len(selected)
            or identity in selected
            or any(n not in gold_clusters for n in selected)
        ):
            raise ValueError("Неверные соседи в выдаче")
        cluster = gold_clusters[identity]
        count = relevant[cluster] - 1 if cluster is not None else 0
        true_hits = sum(
            cluster is not None and gold_clusters[n] == cluster for n in selected
        )
        returned += len(selected)
        hits += true_hits
        possible += count
        if count:
            queries += 1
            query_hits += true_hits > 0
    return {
        "k": k,
        "returned_neighbors": returned,
        "relevant_neighbors": possible,
        "precision_at_k": hits / (len(ranked) * k) if ranked else 0.0,
        "returned_precision_at_k": hits / returned if returned else 0.0,
        "recall_at_k": hits / possible if possible else 0.0,
        "queries_with_repeats": queries,
        "query_hit_fraction": query_hits / queries if queries else None,
    }
