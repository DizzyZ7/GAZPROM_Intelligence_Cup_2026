"""Сопоставление новых кластеров с ранее сохраненными сигналами."""
import json
from collections import defaultdict

def validate_existing_context(store, classified):
    """Проверить все прежние источники, включая новые группы меньше порога."""
    by_id = {row["id"]: row for row in classified}
    linked = defaultdict(set)
    for source in store.db.execute("SELECT signal_id,ticket_id FROM signal_sources"):
        linked[source["signal_id"]].add(source["ticket_id"])
    for signal in store.db.execute("SELECT id,product,category,workflow,payload FROM signals"):
        payload = json.loads(signal["payload"])
        contexts = set()
        for ticket_id in payload.get("ticket_ids", []):
            if ticket_id not in linked[signal["id"]]:
                continue
            row = by_id.get(ticket_id)
            if row is None or row["product"] != signal["product"]:
                continue
            if row["reason"] == "resolved_issue":
                raise ValueError("В сохраненном сигнале есть указание на устранение сбоя. Требуется проверка аналитиком; пакет не сохранен")
            if row["reason"] != "context_resolved" or (row["category"], row["workflow"]) != (signal["category"], signal["workflow"]):
                raise ValueError("Изменение контекста прежнего источника требует согласования аналитика")
            context = (row["operation"], row["symptom"])
            contexts.add(context)
            if payload.get("context") and payload["context"] != dict(zip(("operation", "symptom"), context)):
                raise ValueError("Изменение контекста сигнала требует согласования аналитика")
        if len(contexts) > 1:
            raise ValueError("Разделение прежнего сигнала требует согласования аналитика")


def reconcile(store, signals):
    """Привязать кластер к прежнему ID при пересечении источников.

    Не объединяет разные продукты, категории или сценарии. При неоднозначности
    отказывает в автоматическом сопоставлении вместо слияния сигналов.
    """
    existing = store.db.execute(
        "SELECT id,product,category,workflow,payload FROM signals"
    ).fetchall()
    excluded_by_signal = defaultdict(set)
    for row in store.db.execute("SELECT signal_id,ticket_id FROM excluded_sources"):
        excluded_by_signal[row["signal_id"]].add(row["ticket_id"])
    used = set()
    output = []
    for signal in signals:
        incoming = set(signal.get("ticket_ids", []))
        candidates = []
        excluded_only = False
        for row in existing:
            if row["product"] != signal["product"]:
                continue
            payload = json.loads(row["payload"])
            previous = set(payload.get("ticket_ids", []))
            overlap = len(incoming & previous)
            if (row["product"],row["category"],row["workflow"]) != (
                signal["product"],signal["category"],signal["workflow"]):
                if overlap:
                    raise ValueError("Изменение категории или сценария сигнала требует согласования аналитика")
                continue
            if payload.get("context") and signal.get("context") and payload["context"] != signal["context"]:
                if overlap:
                    raise ValueError("Изменение контекста сигнала требует согласования аналитика")
                continue
            if overlap:
                candidates.append((overlap,row["id"]))
            elif incoming and incoming.issubset(excluded_by_signal.get(row["id"], ())):
                excluded_only = True
        if excluded_only and not candidates:
            # Точный повтор исключенных источников не должен создавать новый сигнал.
            continue
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
