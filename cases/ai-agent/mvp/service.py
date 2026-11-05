"""Единая точка импорта и анализа обращений для CLI и API."""
from pathlib import Path
from hashlib import sha256
import json
from pulse import Ticket, analyze
from pipeline import analyze_semantic
from store import SignalStore
from cluster_identity import reconcile
from benchmark.engine import analyze_method
from benchmark.tfidf import TfidfModel


def unknown_signals(tickets):
    from pulse import classify, redact

    candidates = [t for t in tickets if classify(t.text)[0] == "unknown"]
    if len(candidates) < 2:
        return []
    model = TfidfModel.fit([redact(t.text) for t in candidates])
    model = TfidfModel(
        {term: weight for term, weight in model.idf.items() if term.startswith("c")},
        model.document_count,
    )
    result = analyze_method(
        "tfidf", candidates, 0.70, tfidf=model, include_unknown=True
    )
    by_id = {t.id: t for t in candidates}
    return [
        {
            "id": "experimental:" + sha256(json.dumps(group).encode("utf-8")).hexdigest(),
            "product": by_id[group[0]].product,
            "category": "unknown",
            "workflow": "new_topic",
            "ticket_count": len(group),
            "ticket_ids": group,
            "examples": [redact(by_id[i].text)[:180] for i in group[:2]],
            "method": "experimental_char_tfidf",
        }
        for group in result["groups"]
        if len(group) >= 2
    ]

class ExactTokenProvider:
    """Детерминированный демонстрационный провайдер, не является ML-моделью."""
    def embed(self, texts):
        from semantic import normalize
        vocabulary = sorted({word for text in texts for word in normalize(text).split()})
        return [[float(word in normalize(text).split()) for word in vocabulary] or [0.0] for text in texts]

def ingest_batch(store, tickets, mode="rules", provider=None, threshold=0.86):
    """Накопительный атомарный импорт для API и CLI, без внешних вызовов по умолчанию."""
    if mode not in ("rules", "semantic", "experimental"):
        raise ValueError("Неизвестный режим анализа")
    if mode == "semantic" and provider is None:
        raise ValueError("Для семантического режима нужен провайдер эмбеддингов")
    if any(not isinstance(value, str) or not value.strip()
           for ticket in tickets for value in (ticket.id, ticket.product, ticket.text, ticket.channel)):
        raise ValueError("Поля обращения должны быть непустыми строками")
    if len({ticket.id for ticket in tickets}) != len(tickets):
        raise ValueError("Повторяющиеся идентификаторы обращений")
    with store.atomic():
        staged = store.stage_tickets(tickets)
        accumulated = store.staged_tickets()
        if mode == "semantic":
            result = analyze_semantic(accumulated, provider, threshold=threshold)
        else:
            result = analyze(accumulated)
            if mode == "experimental":
                result["signals"].extend(unknown_signals(accumulated))
        # Смена метода анализа не должна обходить запрет на слияние старых сигналов.
        signals = reconcile(store, result["signals"])
        inserted = store.ingest(signals)
    return {"processed": len(tickets), "staged_new": staged,
            "analyzed_total": result["processed"], "signals_found": len(signals),
            "new_signals": inserted, "mode": mode}


def import_tickets(db_path, file_path, mode="rules", provider=None, threshold=0.86):
    raw = json.loads(Path(file_path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("Ожидается список обращений")
    tickets = [Ticket(**row) for row in raw]
    store = SignalStore(db_path)
    try:
        return ingest_batch(store, tickets, mode, provider, threshold)
    finally:
        store.close()
