"""Пересчет состава только затронутых обновлением источников сигналов."""

import json

from evidence import get_signal
from product_analysis import analyze_product
from product_context import ANALYSIS_VERSION


def capture_signals(store, ticket_ids):
    found = {}
    cached = {}
    for ticket_id in ticket_ids:
        for row in store.db.execute(
            "SELECT signal_id FROM signal_sources WHERE ticket_id=?", (ticket_id,)
        ):
            signal_id = row["signal_id"]
            if signal_id not in cached:
                cached[signal_id] = get_signal(store, signal_id)
            card = cached[signal_id]
            if ticket_id in card["ticket_ids"]:
                found[card["id"]] = card
    return found


def _key(row):
    if row["reason"] != "context_resolved":
        return None
    return (row["category"], row["workflow"], row["operation"], row["symptom"])


def detach_incompatible_sources(store, updates, result, before, *, mode, threshold):
    from store import utcnow

    changed = {u["ticket_id"]: u for u in updates}
    classified = {r["id"]: r for r in result["classified"]}
    tickets = {t.id: t for t in store.staged_tickets()}
    reasons = {}
    for signal_id, card in before.items():
        active_changes = set(card["ticket_ids"]) & changed.keys()
        if not active_changes:
            continue
        raw = store.db.execute(
            "SELECT payload FROM signals WHERE id=?", (signal_id,)
        ).fetchone()
        payload = json.loads(raw["payload"])
        if payload.get("method") == "complete_link_embeddings" and (
            mode != "semantic" or threshold != payload.get("threshold")
        ):
            raise ValueError(
                "Обновление семантического сигнала требует полного анализа с прежним порогом"
            )
        context = payload.get("context")
        if context:
            old_key = (
                card["category"],
                card["workflow"],
                context["operation"],
                context["symptom"],
            )
        else:
            previous = [
                changed[i]["previous"] if i in changed else tickets[i]
                for i in card["ticket_ids"]
                if i in tickets
            ]
            old_rows = analyze_product(previous)["classified"]
            keys = {_key(r) for r in old_rows}
            if len(keys) != 1 or None in keys:
                raise ValueError(
                    "Изменение старого смешанного сигнала требует согласования аналитика"
                )
            old_key = keys.pop()
            if old_key[:2] != (card["category"], card["workflow"]):
                raise ValueError(
                    "Изменение сценария старого сигнала требует согласования аналитика"
                )
        if not context:
            payload["context"] = {"operation": old_key[2], "symptom": old_key[3]}
            payload["analysis_version"] = ANALYSIS_VERSION
        removed = set()
        for ticket_id in active_changes:
            row = classified[ticket_id]
            compatible = _key(row) == old_key
            reasons[(signal_id, ticket_id)] = (
                "source_updated"
                if compatible
                else "resolved_issue"
                if row["reason"] == "resolved_issue"
                else "context_changed"
            )
            if not compatible:
                removed.add(ticket_id)
                store.db.execute(
                    "DELETE FROM signal_sources WHERE signal_id=? AND ticket_id=?",
                    (signal_id, ticket_id),
                )
        remaining = [i for i in card["ticket_ids"] if i not in removed]
        if payload.get("method") == "complete_link_embeddings" and len(remaining) >= 2:
            if not any(
                set(remaining).issubset(s["ticket_ids"]) for s in result["signals"]
            ):
                raise ValueError(
                    "Обновленные источники не подтверждают прежний семантический кластер; требуется аналитик"
                )
        payload["historical_ticket_ids"] = sorted(
            set(payload.get("historical_ticket_ids", [])) | set(card["ticket_ids"])
        )
        payload["ticket_ids"] = remaining
        payload["ticket_count"] = len(remaining)
        payload["examples"] = [
            s["excerpt"]
            for s in store.sources(signal_id, remaining, card["product"])
            if s["excerpt"]
        ][:2]
        store.db.execute(
            "UPDATE signals SET ticket_count=?,payload=?,updated_at=? WHERE id=?",
            (
                len(remaining),
                json.dumps(payload, ensure_ascii=False),
                utcnow(),
                signal_id,
            ),
        )
    return reasons


def record_signal_changes(store, updates, before, reasons):
    from store import utcnow

    if not updates:
        return []
    changed = {u["ticket_id"]: u for u in updates}
    affected = {
        i: c for i, c in before.items() if set(c["ticket_ids"]) & changed.keys()
    }
    affected.update(
        {i: before.get(i) for i in capture_signals(store, changed) if i not in affected}
    )
    events = []
    for signal_id, old in sorted(affected.items()):
        new = get_signal(store, signal_id)
        old_ids = set(old["ticket_ids"]) if old else set()
        new_ids = set(new["ticket_ids"])
        involved = (old_ids | new_ids) & changed.keys()
        changes = [
            dict(
                ticket_id=i,
                from_version=changed[i]["from_version"],
                to_version=changed[i]["to_version"],
                reason=reasons.get((signal_id, i), "source_added"),
            )
            for i in sorted(involved)
        ]
        event = {
            "type": "ticket_updated",
            "signal_id": signal_id,
            "updates": changes,
            "sources_before": len(old_ids),
            "sources_after": len(new_ids),
            "added_ticket_ids": sorted(new_ids - old_ids),
            "removed_ticket_ids": sorted(old_ids - new_ids),
        }
        store.db.execute(
            "INSERT INTO signal_change_events(signal_id,payload,created_at) VALUES (?,?,?)",
            (signal_id, json.dumps(event, ensure_ascii=False), utcnow()),
        )
        events.append(event)
    return events
