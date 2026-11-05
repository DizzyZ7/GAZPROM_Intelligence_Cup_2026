"""Сопоставление новых кластеров с ранее сохраненными сигналами."""

import json
from collections import defaultdict


def _scope(signal):
    return signal["product"], signal["category"], signal["workflow"]


def ensure_compatible(previous, incoming):
    old_context = previous.get("context")
    new_context = incoming.get("context")
    old_embedding = previous.get("method") == "complete_link_embeddings"
    if old_context is None and new_context is None and not old_embedding:
        return

    previous_ids = set(previous.get("ticket_ids", []))
    incoming_ids = set(incoming.get("ticket_ids", []))
    if _scope(previous) != _scope(incoming) or not previous_ids <= incoming_ids:
        raise ValueError(
            "Изменение состава или области сигнала требует согласования аналитика"
        )
    if (
        previous_ids != incoming_ids
        and old_embedding
        and (
            not previous.get("embedding_revision")
            or any(
                previous.get(field) != incoming.get(field)
                for field in ("method", "threshold", "embedding_revision")
            )
        )
    ):
        raise ValueError("Расширение сигнала требует прежних семантических проверок")
    if old_context is None:
        return
    if new_context is None and previous_ids == incoming_ids:
        return
    if (old_context, previous.get("analysis_version")) != (
        new_context,
        incoming.get("analysis_version"),
    ):
        raise ValueError("Изменение контекста сигнала требует согласования аналитика")


def reconcile(store, signals, *, analyzed_ids=frozenset(), context_mode=False):
    """Привязать новый кластер к прежнему ID при совместимом наборе источников."""
    existing = {}
    by_source = defaultdict(set)
    for row in store.db.execute(
        "SELECT id,product,category,workflow,payload FROM signals"
    ):
        signal = dict(
            json.loads(row["payload"]),
            id=row["id"],
            product=row["product"],
            category=row["category"],
            workflow=row["workflow"],
        )
        existing[signal["id"]] = signal
        for ticket_id in signal.get("ticket_ids", []):
            by_source[signal["product"], ticket_id].add(signal["id"])

    used = set()
    output = []
    for signal in signals:
        overlaps = set()
        for ticket_id in signal.get("ticket_ids", []):
            overlaps.update(by_source.get((signal["product"], ticket_id), ()))
        candidates = {
            identity
            for identity in overlaps
            if _scope(existing[identity]) == _scope(signal)
            or "context" in signal
            or "context" in existing[identity]
        }
        if signal["id"] in existing:
            candidates.add(signal["id"])
        if len(candidates) > 1:
            raise ValueError(
                "Объединение существующих сигналов требует согласования аналитика"
            )
        if candidates:
            identity = candidates.pop()
            ensure_compatible(existing[identity], signal)
            signal = dict(signal, id=identity)
        if signal["id"] in used:
            raise ValueError("Несколько новых кластеров соответствуют одному сигналу")
        used.add(signal["id"])
        output.append(signal)
    for identity in existing.keys() - used:
        previous = existing[identity]
        if not (
            context_mode
            or "context" in previous
            or previous.get("method") == "complete_link_embeddings"
        ):
            continue
        previous_ids = previous.get("ticket_ids", [])
        if previous_ids and analyzed_ids.issuperset(previous_ids):
            raise ValueError(
                "Сигнал исчез из результатов анализа; требуется согласование аналитика"
            )
    return output
