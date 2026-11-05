"""Единая точка импорта и анализа обращений для CLI и API."""

import json
from pathlib import Path

from cluster_identity import reconcile
from pipeline import analyze_contextual, analyze_semantic
from pulse import Ticket, analyze
from store import SignalStore


class ExactTokenProvider:
    """Детерминированный демонстрационный провайдер, не является ML-моделью."""

    def embed(self, texts):
        from semantic import normalize

        vocabulary = sorted(
            {word for text in texts for word in normalize(text).split()}
        )
        return [
            [float(word in normalize(text).split()) for word in vocabulary] or [0.0]
            for text in texts
        ]


def ingest_batch(
    store,
    tickets,
    mode="rules",
    provider=None,
    threshold=0.86,
    *,
    embedding_revision=None,
):
    """Накопительный атомарный импорт для API и CLI, без внешних вызовов по умолчанию."""
    if mode not in ("rules", "context", "semantic"):
        raise ValueError("Неизвестный режим анализа")
    if mode == "semantic" and provider is None:
        raise ValueError("Для семантического режима нужен провайдер эмбеддингов")
    if embedding_revision is not None and (
        mode != "semantic"
        or not isinstance(embedding_revision, str)
        or not 0 < len(embedding_revision.strip()) <= 256
        or embedding_revision != embedding_revision.strip()
    ):
        raise ValueError("Некорректный идентификатор ревизии эмбеддингов")
    if any(
        not isinstance(value, str) or not value.strip()
        for ticket in tickets
        for value in (ticket.id, ticket.product, ticket.text, ticket.channel)
    ):
        raise ValueError("Поля обращения должны быть непустыми строками")
    requested_ids = {ticket.id for ticket in tickets}
    if len(requested_ids) != len(tickets):
        raise ValueError("Повторяющиеся идентификаторы обращений")
    with store.atomic():
        staged = store.stage_tickets(tickets)
        accumulated = store.staged_tickets()
        if mode == "semantic":
            result = analyze_semantic(accumulated, provider, threshold=threshold)
            if embedding_revision is not None:
                for signal in result["signals"]:
                    signal["embedding_revision"] = embedding_revision
        elif mode == "context":
            result = analyze_contextual(accumulated)
        else:
            result = analyze(accumulated)
        # Смена метода анализа не должна обходить запрет на слияние старых сигналов.
        signals = reconcile(
            store,
            result["signals"],
            analyzed_ids={ticket.id for ticket in accumulated},
            context_mode=mode != "rules",
        )
        inserted = store.ingest(signals)
    response = {
        "processed": len(tickets),
        "staged_new": staged,
        "analyzed_total": result["processed"],
        "signals_found": len(signals),
        "new_signals": inserted,
        "mode": mode,
    }
    if mode != "rules":
        grouped_ids = {
            identity for signal in signals for identity in signal["ticket_ids"]
        }
        ungrouped_ids = requested_ids - grouped_ids
        response["ungrouped"] = [
            {
                "ticket_id": row["id"],
                "reason": row["reason"]
                if row["reason"] != "context_resolved"
                else "no_matching_cluster",
            }
            for row in result["classified"]
            if row["id"] in ungrouped_ids
        ]
    return response


def import_tickets(
    db_path,
    file_path,
    mode="rules",
    provider=None,
    threshold=0.86,
    *,
    embedding_revision=None,
):
    raw = json.loads(Path(file_path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("Ожидается список обращений")
    tickets = [Ticket(**row) for row in raw]
    store = SignalStore(db_path)
    try:
        return ingest_batch(
            store,
            tickets,
            mode,
            provider,
            threshold,
            embedding_revision=embedding_revision,
        )
    finally:
        store.close()
