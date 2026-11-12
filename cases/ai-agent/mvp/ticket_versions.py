"""Неизменяемые обезличенные версии обращений и монотонная текущая версия."""

from contextlib import nullcontext

from pulse import Ticket, redact

MAX_VERSION = 2**63 - 1


def initialize(store):
    store.db.executescript("""
        CREATE TABLE IF NOT EXISTS ticket_versions (
          ticket_id TEXT NOT NULL REFERENCES staged_tickets(id),
          version INTEGER NOT NULL CHECK(version > 0),
          product TEXT NOT NULL, text TEXT NOT NULL, channel TEXT NOT NULL,
          imported_at TEXT NOT NULL, PRIMARY KEY(ticket_id,version)
        );
        CREATE TABLE IF NOT EXISTS ticket_heads (
          ticket_id TEXT PRIMARY KEY REFERENCES staged_tickets(id),
          version INTEGER NOT NULL,
          FOREIGN KEY(ticket_id,version) REFERENCES ticket_versions(ticket_id,version)
        );
        CREATE TABLE IF NOT EXISTS signal_change_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          signal_id TEXT NOT NULL REFERENCES signals(id),
          payload TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS changes_by_signal ON signal_change_events(signal_id,id);
        CREATE TRIGGER IF NOT EXISTS immutable_ticket_version_update
          BEFORE UPDATE ON ticket_versions BEGIN
          SELECT RAISE(ABORT,'Ticket versions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_ticket_version_delete
          BEFORE DELETE ON ticket_versions BEGIN
          SELECT RAISE(ABORT,'Ticket versions are immutable'); END;
    """)
    with store.atomic():
        for row in store.db.execute(
            "SELECT t.* FROM staged_tickets t LEFT JOIN ticket_heads h ON h.ticket_id=t.id WHERE h.ticket_id IS NULL"
        ).fetchall():
            safe = redact(row["text"])
            store.db.execute(
                "UPDATE staged_tickets SET text=? WHERE id=?", (safe, row["id"])
            )
            store.db.execute(
                "INSERT INTO ticket_versions VALUES (?,?,?,?,?,?)",
                (row["id"], 1, row["product"], safe, row["channel"], row["created_at"]),
            )
            store.db.execute("INSERT INTO ticket_heads VALUES (?,1)", (row["id"],))


def stage_versions(store, tickets, versions=None, ticket_dates=None):
    from store import utcnow

    versions = versions or {}
    ticket_dates = ticket_dates or {}
    ids = {t.id for t in tickets}
    if len(ids) != len(tickets) or set(versions) - ids or set(ticket_dates) - ids:
        raise ValueError("Неверные идентификаторы в пакете версий")
    if any(type(v) is not int or not 1 <= v <= MAX_VERSION for v in versions.values()):
        raise ValueError("Версия должна быть целым числом от 1 до 2^63-1")
    if any(
        not isinstance(v, str) or not v.strip()
        for t in tickets
        for v in (t.id, t.product, t.text, t.channel)
    ):
        raise ValueError("Поля обращения должны быть непустыми строками")
    inserted, updates = 0, []
    with nullcontext() if store.db.in_transaction else store.atomic():
        for ticket in tickets:
            safe = redact(ticket.text)
            row = store.db.execute(
                "SELECT t.*,h.version FROM staged_tickets t JOIN ticket_heads h ON h.ticket_id=t.id WHERE t.id=?",
                (ticket.id,),
            ).fetchone()
            now = utcnow()
            if row is None:
                version = versions.get(ticket.id, 1)
                store.db.execute(
                    "INSERT INTO staged_tickets VALUES (?,?,?,?,?)",
                    (ticket.id, ticket.product, safe, ticket.channel, now),
                )
                inserted += 1
            else:
                if (row["product"], row["channel"]) != (ticket.product, ticket.channel):
                    raise ValueError(
                        "Продукт и канал существующего обращения менять нельзя"
                    )
                version = versions.get(ticket.id, row["version"])
                if version < row["version"]:
                    raise ValueError(
                        "Устаревшая версия обращения; новая версия уже сохранена"
                    )
                if version == row["version"]:
                    if safe != row["text"]:
                        raise ValueError(
                            "Идентификатор и версия обращения уже используются с другим содержимым"
                        )
                    if ticket.id in ticket_dates:
                        store._stage_ticket_date(ticket.id, ticket_dates[ticket.id])
                    continue
                updates.append(
                    {
                        "ticket_id": ticket.id,
                        "from_version": row["version"],
                        "to_version": version,
                        "previous": Ticket(
                            ticket.id, row["product"], row["text"], row["channel"]
                        ),
                    }
                )
                store.db.execute(
                    "UPDATE staged_tickets SET text=? WHERE id=?", (safe, ticket.id)
                )
            store.db.execute(
                "INSERT INTO ticket_versions VALUES (?,?,?,?,?,?)",
                (ticket.id, version, ticket.product, safe, ticket.channel, now),
            )
            store.db.execute(
                "INSERT INTO ticket_heads VALUES (?,?) ON CONFLICT(ticket_id) DO UPDATE SET version=excluded.version",
                (ticket.id, version),
            )
            if ticket.id in ticket_dates:
                store._stage_ticket_date(ticket.id, ticket_dates[ticket.id])
    return {"inserted": inserted, "updates": updates}


def history(store, ticket_id):
    rows = store.db.execute(
        "SELECT version,product,text,channel,imported_at FROM ticket_versions WHERE ticket_id=? ORDER BY version",
        (ticket_id,),
    ).fetchall()
    if not rows:
        raise KeyError(ticket_id)
    return [dict(row) for row in rows]
