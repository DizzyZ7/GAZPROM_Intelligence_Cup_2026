"""Единая точка импорта и анализа обращений для CLI и API."""
from pathlib import Path
import json
from pulse import Ticket, analyze
from pipeline import analyze_semantic
from store import SignalStore
from cluster_identity import reconcile
from issue_status import filter_resolved

class ExactTokenProvider:
    """Детерминированный демонстрационный провайдер, не является ML-моделью."""
    def embed(self, texts):
        from semantic import normalize
        vocabulary = sorted({word for text in texts for word in normalize(text).split()})
        return [[float(word in normalize(text).split()) for word in vocabulary] or [0.0] for text in texts]

def ingest_batch(store, tickets, mode="rules", provider=None, threshold=0.86, *, ticket_dates=None):
    """Накопительный атомарный импорт для API и CLI, без внешних вызовов по умолчанию."""
    if mode not in ("rules", "semantic"):
        raise ValueError("Неизвестный режим анализа")
    if mode == "semantic" and provider is None:
        raise ValueError("Для семантического режима нужен провайдер эмбеддингов")
    if any(not isinstance(value, str) or not value.strip()
           for ticket in tickets for value in (ticket.id, ticket.product, ticket.text, ticket.channel)):
        raise ValueError("Поля обращения должны быть непустыми строками")
    if len({ticket.id for ticket in tickets}) != len(tickets):
        raise ValueError("Повторяющиеся идентификаторы обращений")
    with store.atomic():
        staged = store.stage_tickets(tickets, ticket_dates=ticket_dates)
        accumulated = store.staged_tickets()
        if mode == "semantic":
            result = analyze_semantic(accumulated, provider, threshold=threshold)
        else:
            eligible, resolved = filter_resolved(accumulated)
            result = analyze(eligible)
            result["resolved_tickets"] = resolved
        # Смена метода анализа не должна обходить запрет на слияние старых сигналов.
        signals = reconcile(store, result["signals"])
        inserted = store.ingest(signals)
    return {"processed": len(tickets), "staged_new": staged,
            "analyzed_total": len(accumulated), "signals_found": len(signals),
            "resolved_tickets": result["resolved_tickets"],
            "new_signals": inserted, "mode": mode}


def import_tickets(db_path, file_path, mode="rules", provider=None, threshold=0.86):
    raw = json.loads(Path(file_path).read_text(encoding="utf-8"))
    if not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw):
        raise ValueError("Ожидается список обращений")
    ticket_dates = {}
    tickets = []
    for row in raw:
        data = dict(row)
        occurred_at = data.pop("occurred_at", None)
        ticket = Ticket(**data)
        tickets.append(ticket)
        if occurred_at is not None:
            ticket_dates[ticket.id] = occurred_at
    store = SignalStore(db_path)
    try:
        return ingest_batch(store, tickets, mode, provider, threshold, ticket_dates=ticket_dates)
    finally:
        store.close()
