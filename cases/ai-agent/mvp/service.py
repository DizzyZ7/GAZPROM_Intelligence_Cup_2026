"""Единая точка импорта и анализа обращений для CLI и API."""
from pathlib import Path
import json
from pulse import Ticket, analyze
from pipeline import analyze_semantic
from store import SignalStore
from cluster_identity import reconcile

class ExactTokenProvider:
    """Детерминированный демонстрационный провайдер, не является ML-моделью."""
    def embed(self, texts):
        from semantic import normalize
        vocabulary = sorted({word for text in texts for word in normalize(text).split()})
        return [[float(word in normalize(text).split()) for word in vocabulary] or [0.0] for text in texts]

def import_tickets(db_path, file_path, mode="rules", provider=None, threshold=0.86):
    if mode not in ("rules", "semantic"):
        raise ValueError("Неизвестный режим анализа")
    raw = json.loads(Path(file_path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("Ожидается список обращений")
    tickets = [Ticket(**row) for row in raw]
    if mode == "semantic":
        if provider is None:
            raise ValueError("Для семантического режима нужен провайдер эмбеддингов")
        result = analyze_semantic(tickets, provider, threshold=threshold)
    else:
        result = analyze(tickets)
    store = SignalStore(db_path)
    try:
        signals = reconcile(store, result["signals"]) if mode == "semantic" else result["signals"]
        inserted = store.ingest(signals)
    finally:
        store.close()
    return {"processed": result["processed"], "signals_found": len(result["signals"]),
            "new_signals": inserted, "mode": mode}
