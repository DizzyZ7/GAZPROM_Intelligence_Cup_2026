"""Сопоставление новых кластеров с ранее сохраненными сигналами."""
import json

def reconcile(store, signals):
    """Привязать кластер к прежнему ID при пересечении источников.

    Не объединяет разные продукты, категории или сценарии. При неоднозначности
    отказывает в автоматическом сопоставлении вместо слияния сигналов.
    """
    existing = store.db.execute(
        "SELECT id,product,category,workflow,payload FROM signals"
    ).fetchall()
    used = set()
    output = []
    for signal in signals:
        incoming = set(signal.get("ticket_ids", []))
        candidates = []
        for row in existing:
            if (row["product"],row["category"],row["workflow"]) != (
                signal["product"],signal["category"],signal["workflow"]):
                continue
            previous = set(json.loads(row["payload"]).get("ticket_ids", []))
            overlap = len(incoming & previous)
            if overlap:
                candidates.append((overlap,row["id"]))
        candidates.sort(key=lambda item:(-item[0],item[1]))
        if candidates:
            if len(candidates) > 1:
                raise ValueError("Объединение существующих сигналов требует согласования аналитика")
            identity = candidates[0][1]
            if identity in used:
                raise ValueError("Несколько новых кластеров соответствуют одному сигналу")
            signal = dict(signal,id=identity)
        if signal["id"] in used:
            raise ValueError("Повторный идентификатор кластера")
        used.add(signal["id"])
        output.append(signal)
    return output
