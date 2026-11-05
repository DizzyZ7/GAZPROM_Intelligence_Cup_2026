"""Чтение карточек сигналов для панели аналитика."""
import json

def get_signal(store, signal_id):
    row = store.db.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
    if row is None:
        raise KeyError(signal_id)
    payload = json.loads(row["payload"])
    detail = {
        "id": row["id"],
        "product": row["product"],
        "category": row["category"],
        "workflow": row["workflow"],
        "ticket_count": row["ticket_count"],
        "ticket_ids": payload.get("ticket_ids", []),
        "examples": payload.get("examples", []),
        "method": payload.get("method", "rules"),
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if "context" in payload:
        detail.update(context=payload["context"], analysis_version=payload["analysis_version"])
        if "threshold" in payload:
            detail["threshold"] = payload["threshold"]
        if "embedding_revision" in payload:
            detail["embedding_revision"] = payload["embedding_revision"]
    return detail
