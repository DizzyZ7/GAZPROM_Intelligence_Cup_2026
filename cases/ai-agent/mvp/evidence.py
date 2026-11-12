"""Чтение карточек сигналов для панели аналитика."""
import hashlib
import json

def get_signal(store, signal_id):
    if store.db.in_transaction:
        return _read_signal(store, signal_id)
    store.db.execute("BEGIN")
    with store.db:
        return _read_signal(store, signal_id)

def _read_signal(store, signal_id):
    row = store.db.execute("""
        SELECT s.*, q.snapshot,
          (SELECT COUNT(*) FROM evidence_events e WHERE e.signal_id=s.id) +
          (SELECT COUNT(*) FROM excluded_sources x WHERE x.signal_id=s.id) AS evidence_generation
        FROM signals s LEFT JOIN product_queue q ON q.signal_id=s.id
        WHERE s.id=?
    """, (signal_id,)).fetchone()
    if row is None:
        raise KeyError(signal_id)
    payload = json.loads(row["payload"])
    ticket_ids = payload.get("ticket_ids", [])
    sources = store.sources(signal_id, ticket_ids, row["product"])
    examples = list(dict.fromkeys(source["excerpt"] for source in sources if source["excerpt"]))[:2]
    evidence = {
        "id": row["id"],
        "product": row["product"],
        "category": row["category"],
        "workflow": row["workflow"],
        "ticket_count": row["ticket_count"],
        "ticket_ids": ticket_ids,
        "examples": examples or payload.get("examples", []),
        "sources": sources,
        "method": payload.get("method", "rules"),
    }
    for key in ("context", "analysis_version"):
        if key in payload:
            evidence[key] = payload[key]
    # Добавление и последующее исключение источника не восстанавливают старую версию.
    content = json.dumps(
        [row["evidence_generation"], evidence],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return dict(
        evidence, revision=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        reviewed_revision=json.loads(row["snapshot"])["revision"] if row["snapshot"] else None,
        status=row["status"], created_at=row["created_at"], updated_at=row["updated_at"],
    )
