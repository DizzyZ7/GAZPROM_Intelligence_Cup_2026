"""Группировка по наблюдаемому контексту с дополнительной проверкой сходства."""

import hashlib
import json
from collections import defaultdict
from math import isfinite

from pulse import Ticket
from semantic import EmbeddingProvider, cosine
from ticket_context import (
    ACTIONABLE,
    ANALYSIS_VERSION,
    classify_context,
    sanitized_text,
)


def _clusters(members, vectors, threshold):
    if vectors is None:
        return [members]
    clusters = []
    for ticket in members:
        for cluster in clusters:
            if all(
                cosine(vectors[ticket.id], vectors[other.id]) >= threshold
                for other in cluster
            ):
                cluster.append(ticket)
                break
        else:
            clusters.append([ticket])
    return clusters


def _embedding_vectors(provider, members):
    if not members:
        return {}
    vectors = provider.embed([ticket.text for ticket in members])
    if not isinstance(vectors, list) or len(vectors) != len(members):
        raise ValueError("Провайдер вернул неверное число векторов")
    dimensions = None
    for vector in vectors:
        if (
            not isinstance(vector, list)
            or not vector
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                for value in vector
            )
        ):
            raise ValueError("Провайдер вернул некорректный вектор")
        if dimensions is None:
            dimensions = len(vector)
        if len(vector) != dimensions:
            raise ValueError("Размерности векторов различаются")
        cosine(vector, vector)
    return {ticket.id: vector for ticket, vector in zip(members, vectors)}


def analyze_contextual(
    tickets: list[Ticket],
    *,
    provider: EmbeddingProvider | None = None,
    threshold: float = 0.86,
    min_tickets: int = 2,
) -> dict:
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not 0 < threshold <= 1
        or type(min_tickets) is not int
        or min_tickets < 2
    ):
        raise ValueError("Некорректный порог или минимальное число обращений")
    if any(
        not isinstance(value, str) or not value.strip()
        for ticket in tickets
        for value in (ticket.id, ticket.product, ticket.text, ticket.channel)
    ):
        raise ValueError("Поля обращения должны быть непустыми строками")
    if len({ticket.id for ticket in tickets}) != len(tickets):
        raise ValueError("Повторяющиеся идентификаторы обращений")

    buckets = defaultdict(list)
    classified = []
    candidates = []
    for ticket in sorted(tickets, key=lambda item: item.id):
        safe = Ticket(
            ticket.id, ticket.product, sanitized_text(ticket.text), ticket.channel
        )
        item = classify_context(safe.text)
        classified.append(
            {
                "id": ticket.id,
                "product": ticket.product,
                "category": item.category,
                "workflow": item.workflow,
                "operation": item.operation,
                "symptom": item.symptom,
                "reason": item.reason,
                "needs_review": item.category == "unknown"
                or (item.category in ACTIONABLE and not item.groupable),
            }
        )
        if item.groupable:
            buckets[
                (
                    ticket.product,
                    item.category,
                    item.workflow,
                    item.operation,
                    item.symptom,
                )
            ].append(safe)
            candidates.append(safe)

    vectors = _embedding_vectors(provider, candidates) if provider is not None else None
    signals = []
    for key, members in sorted(buckets.items()):
        product, category, workflow, operation, symptom = key
        for cluster in _clusters(members, vectors, threshold):
            if len(cluster) < min_tickets:
                continue
            identity = (key, cluster[0].id) if vectors is not None else key
            digest = hashlib.sha256(
                json.dumps(identity, ensure_ascii=False).encode()
            ).hexdigest()
            signal = {
                "id": f"ctx-{digest[:32]}",
                "product": product,
                "category": category,
                "workflow": workflow,
                "context": {"operation": operation, "symptom": symptom},
                "analysis_version": ANALYSIS_VERSION,
                "ticket_ids": [ticket.id for ticket in cluster],
                "ticket_count": len(cluster),
                "examples": [ticket.text[:180] for ticket in cluster[:2]],
                "status": "pending_human_review",
                "method": "complete_link_embeddings"
                if vectors is not None
                else "context_rules",
            }
            if vectors is not None:
                signal["threshold"] = threshold
            signals.append(signal)
    return {
        "processed": len(tickets),
        "signals": signals,
        "classified": classified,
        "unclassified": sum(row["category"] == "unknown" for row in classified),
    }


def analyze_semantic(tickets, provider, threshold=0.86, min_tickets=2):
    if provider is None:
        raise ValueError("Семантический анализ требует провайдер эмбеддингов")
    return analyze_contextual(
        tickets, provider=provider, threshold=threshold, min_tickets=min_tickets
    )
