"""SQLite review queue for offline demo; not a production authorization layer."""
import json
import re
import sqlite3
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path

from evidence import get_signal
from pulse import Ticket, redact

STATES = {'pending_human_review', 'approved', 'rejected'}
OWNER_DECISIONS = {'in_progress', 'known', 'needs_clarification'}
TICKET_DATE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)

def utcnow():
    return datetime.now(timezone.utc).isoformat()

def normalize_ticket_date(value):
    try:
        if not isinstance(value, str) or not TICKET_DATE.fullmatch(value):
            raise ValueError
        return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError) as exc:
        raise ValueError("Дата обращения должна быть в ISO 8601 с часовым поясом") from exc

class SignalStore:
    def __init__(self, path):
        self.path = str(Path(path))
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS signals (
          id TEXT PRIMARY KEY, product TEXT NOT NULL, category TEXT NOT NULL,
          workflow TEXT NOT NULL, ticket_count INTEGER NOT NULL,
          payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending_human_review',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS review_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id TEXT NOT NULL,
          actor TEXT NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY(signal_id) REFERENCES signals(id)
        );
        CREATE TABLE IF NOT EXISTS evidence_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          signal_id TEXT NOT NULL,
          added_ticket_ids TEXT NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY(signal_id) REFERENCES signals(id)
        );
        CREATE INDEX IF NOT EXISTS evidence_events_by_signal ON evidence_events(signal_id);
        CREATE TABLE IF NOT EXISTS staged_tickets (
          id TEXT PRIMARY KEY, product TEXT NOT NULL, text TEXT NOT NULL,
          channel TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ticket_dates (
          ticket_id TEXT PRIMARY KEY REFERENCES staged_tickets(id),
          occurred_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS signal_sources (
          signal_id TEXT NOT NULL REFERENCES signals(id),
          ticket_id TEXT NOT NULL REFERENCES staged_tickets(id),
          PRIMARY KEY(signal_id,ticket_id)
        );
        CREATE TABLE IF NOT EXISTS excluded_sources (
          signal_id TEXT NOT NULL REFERENCES signals(id), ticket_id TEXT NOT NULL,
          actor TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(signal_id,ticket_id)
        );
        CREATE TABLE IF NOT EXISTS product_queue (
          signal_id TEXT PRIMARY KEY REFERENCES signals(id),
          first_ticket_at TEXT, delivered_at TEXT NOT NULL,
          decision TEXT, reason TEXT, decided_at TEXT, snapshot TEXT, decided_by TEXT
        );
        ''')
        self._upgrade_queue()

    def _upgrade_queue(self):
        """Сохранить решения из ранней демонстрационной схемы очереди."""
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(product_queue)")}
        if {"snapshot", "decided_by"} <= columns:
            return
        with self.atomic():
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(product_queue)")}
            if "snapshot" not in columns:
                self.db.execute("ALTER TABLE product_queue ADD COLUMN snapshot TEXT")
            if "decided_by" not in columns:
                self.db.execute("ALTER TABLE product_queue ADD COLUMN decided_by TEXT")

    @contextmanager
    def atomic(self):
        """Захватить право записи перед чтением состояния для изменения."""
        if self.db.in_transaction:
            raise RuntimeError("Нельзя открыть вложенную транзакцию")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def stage_tickets(self, tickets, *, ticket_dates=None):
        """Идемпотентное сохранение обезличенных обращений."""
        ticket_dates = ticket_dates or {}
        inserted = 0
        with (nullcontext() if self.db.in_transaction else self.atomic()):
            for ticket in tickets:
                sanitized = redact(ticket.text)
                row = self.db.execute(
                    'SELECT product,text,channel FROM staged_tickets WHERE id=?',
                    (ticket.id,)).fetchone()
                if row is not None:
                    if (row['product'],row['text'],row['channel']) != (
                        ticket.product,sanitized,ticket.channel):
                        raise ValueError('Идентификатор обращения уже используется с другим содержимым')
                else:
                    self.db.execute(
                        'INSERT INTO staged_tickets(id,product,text,channel,created_at) VALUES(?,?,?,?,?)',
                        (ticket.id,ticket.product,sanitized,ticket.channel,utcnow()))
                    inserted += 1
                if ticket.id in ticket_dates:
                    self._stage_ticket_date(ticket.id, ticket_dates[ticket.id])
        return inserted

    def _stage_ticket_date(self, ticket_id, value):
        date = normalize_ticket_date(value)
        existing = self.db.execute(
            "SELECT occurred_at FROM ticket_dates WHERE ticket_id=?", (ticket_id,),
        ).fetchone()
        if existing is None:
            self.db.execute(
                "INSERT INTO ticket_dates(ticket_id,occurred_at) VALUES (?,?)", (ticket_id, date),
            )
        elif existing["occurred_at"] != date:
            raise ValueError("Дата обращения не совпадает с сохраненной")

    def staged_tickets(self):
        return [Ticket(row['id'],row['product'],row['text'],row['channel'])
                for row in self.db.execute(
                    'SELECT id,product,text,channel FROM staged_tickets ORDER BY id')]

    def ingest(self, signals):
        """Идемпотентный импорт с накоплением новых источников.

        Решения аналитика и переданные снимки сохраняются при росте сигнала.
        """
        inserted = 0
        with (nullcontext() if self.db.in_transaction else self.atomic()):
            for s in signals:
                incoming_ids = list(dict.fromkeys(s.get('ticket_ids', [])))
                if len(incoming_ids) != s['ticket_count']:
                    raise ValueError('Количество обращений не совпадает со списком источников')
                current = self.db.execute(
                    'SELECT product,category,workflow,payload FROM signals WHERE id=?',
                    (s['id'],)).fetchone()
                if current is None:
                    payload = dict(s, ticket_ids=incoming_ids)
                    self.db.execute('''INSERT INTO signals
                    (id,product,category,workflow,ticket_count,payload,status,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?)''',
                    (s['id'],s['product'],s['category'],s['workflow'],
                     len(incoming_ids),json.dumps(payload,ensure_ascii=False),
                     'pending_human_review',utcnow(),utcnow()))
                    self._link_sources(s['id'], s['product'], incoming_ids)
                    self.db.execute(
                        'INSERT INTO evidence_events(signal_id,added_ticket_ids,created_at) VALUES(?,?,?)',
                        (s['id'],json.dumps(incoming_ids,ensure_ascii=False),utcnow()))
                    inserted += 1
                    continue
                if (current['product'],current['category'],current['workflow']) != (
                    s['product'],s['category'],s['workflow']):
                    raise ValueError('Конфликт идентификатора сигнала')
                old = json.loads(current['payload'])
                excluded = {row["ticket_id"] for row in self.db.execute(
                    "SELECT ticket_id FROM excluded_sources WHERE signal_id=?", (s["id"],),
                )}
                incoming_ids = [ticket_id for ticket_id in incoming_ids if ticket_id not in excluded]
                self._link_sources(s["id"], s["product"], incoming_ids)
                old_ids = old.get("ticket_ids", [])
                known_ids = set(old_ids)
                additions = [item for item in incoming_ids if item not in known_ids]
                if not additions:
                    continue
                merged_ids = old_ids + additions
                self.db.execute(
                    'INSERT INTO evidence_events(signal_id,added_ticket_ids,created_at) VALUES(?,?,?)',
                    (s['id'],json.dumps(additions,ensure_ascii=False),utcnow()))
                old['ticket_ids'] = merged_ids
                old['ticket_count'] = len(merged_ids)
                if excluded:
                    old["examples"] = [
                        source["excerpt"] for source in self.sources(s["id"], merged_ids, s["product"])
                        if source["excerpt"]
                    ][:2]
                else:
                    old['examples'] = list(dict.fromkeys(
                        old.get('examples', []) + s.get('examples', [])))[:2]
                self.db.execute(
                    'UPDATE signals SET ticket_count=?,payload=?,updated_at=? WHERE id=?',
                    (len(merged_ids),json.dumps(old,ensure_ascii=False),utcnow(),s['id']))
        return inserted

    def _link_sources(self, signal_id, product, ticket_ids):
        # Связь создается по результату анализа, а не при поиске похожего ID в старой БД.
        self.db.executemany(
            "INSERT INTO signal_sources(signal_id,ticket_id) "
            "SELECT ?,id FROM staged_tickets WHERE id=? AND product=? "
            "ON CONFLICT(signal_id,ticket_id) DO NOTHING",
            ((signal_id, ticket_id, product) for ticket_id in ticket_ids),
        )

    def list_signals(self, status=None):
        if status is not None and status not in STATES:
            raise ValueError('Unknown status')
        sql = 'SELECT id,product,category,workflow,ticket_count,status FROM signals'
        rows = self.db.execute(sql + (' WHERE status=?' if status else '') + ' ORDER BY id',
                               (status,) if status else ()).fetchall()
        return [dict(r) for r in rows]

    def _require_current_review(self, signal_id, expected_revision):
        card = get_signal(self, signal_id)
        if card["status"] != "pending_human_review":
            raise ValueError("Карточка уже проверена")
        if card["revision"] != expected_revision:
            raise ValueError("Карточка изменилась. Проверьте обновленные доказательства")
        return card

    def review(self, signal_id, action, actor, reason, *, expected_revision):
        if action not in {'approved', 'rejected'}:
            raise ValueError("Недопустимое решение аналитика")
        if not actor.strip() or not reason.strip():
            raise ValueError("Укажите аналитика и основание")
        with self.atomic():
            card = self._require_current_review(signal_id, expected_revision)
            if action == "approved" and card["ticket_count"] < 2:
                raise ValueError("Для подтверждения нужны хотя бы два обращения")
            if action == "approved" and any(source["excerpt"] is None for source in card["sources"]):
                raise ValueError("У части источников отсутствуют связанные фрагменты")
            now = utcnow()
            self.db.execute('UPDATE signals SET status=?,updated_at=? WHERE id=?',
                            (action, now, signal_id))
            self.db.execute('INSERT INTO review_events(signal_id,actor,action,reason,created_at) VALUES(?,?,?,?,?)',
                            (signal_id, actor, action, redact(reason.strip()), now))
            if action == "approved":
                dates = [source["occurred_at"] for source in card["sources"]]
                first_date = min(dates) if dates and all(dates) else None
                snapshot = dict(card, status=action, updated_at=now, reviewed_revision=card["revision"])
                self.db.execute(
                    "INSERT INTO product_queue(signal_id,first_ticket_at,delivered_at,snapshot) "
                    "VALUES (?,?,?,?)",
                    (signal_id, first_date, now, json.dumps(snapshot, ensure_ascii=False)),
                )

    def sources(self, signal_id, ids, product):
        sources = []
        for ticket_id in ids:
            row = self.db.execute(
                "SELECT t.text,d.occurred_at FROM signal_sources s "
                "JOIN staged_tickets t ON t.id=s.ticket_id LEFT JOIN ticket_dates d ON d.ticket_id=t.id "
                "WHERE s.signal_id=? AND s.ticket_id=? AND t.product=?", (signal_id, ticket_id, product),
            ).fetchone()
            sources.append({
                "ticket_id": ticket_id,
                "excerpt": row["text"][:300] if row else None,
                "occurred_at": row["occurred_at"] if row else None,
            })
        return sources

    def exclude_source(self, signal_id, ticket_id, actor, reason, *, expected_revision):
        if not actor.strip() or not reason.strip():
            raise ValueError("Укажите аналитика и основание исключения")
        with self.atomic():
            card = self._require_current_review(signal_id, expected_revision)
            ids = card["ticket_ids"]
            if ticket_id not in ids:
                raise ValueError("Источник не входит в карточку")
            if len(ids) < 2:
                raise ValueError("Исключать можно только из карточки с двумя или более обращениями")
            remaining = [source for source in card["sources"] if source["ticket_id"] != ticket_id]
            row = self.db.execute("SELECT payload FROM signals WHERE id=?", (signal_id,)).fetchone()
            payload = json.loads(row["payload"])
            payload["ticket_ids"] = [source["ticket_id"] for source in remaining]
            payload["ticket_count"] = len(remaining)
            payload["examples"] = [source["excerpt"] for source in remaining if source["excerpt"]][:2]
            now = utcnow()
            self.db.execute(
                "INSERT INTO excluded_sources(signal_id,ticket_id,actor,reason,created_at) "
                "VALUES (?,?,?,?,?)", (signal_id, ticket_id, actor, redact(reason.strip()), now),
            )
            self.db.execute(
                "UPDATE signals SET ticket_count=?,payload=?,updated_at=? WHERE id=?",
                (len(remaining), json.dumps(payload, ensure_ascii=False), now, signal_id),
            )
            return get_signal(self, signal_id)

    def exclusions(self, signal_id):
        return [dict(row) for row in self.db.execute(
            "SELECT ticket_id,actor,reason,created_at FROM excluded_sources WHERE signal_id=? "
            "ORDER BY created_at,ticket_id", (signal_id,),
        )]

    def queue(self):
        result = []
        for row in self.db.execute(
            "SELECT q.*,s.product,s.workflow FROM product_queue q JOIN signals s ON s.id=q.signal_id "
            "ORDER BY q.delivered_at DESC,q.signal_id"
        ):
            result.append(dict(row, snapshot=json.loads(row["snapshot"]) if row["snapshot"] else None))
        return result

    def decide(self, signal_id, decision, actor, reason):
        if decision not in OWNER_DECISIONS or not actor.strip() or not reason.strip():
            raise ValueError("Укажите участника, допустимое решение и основание")
        with self.atomic():
            row = self.db.execute(
                "SELECT decision,snapshot FROM product_queue WHERE signal_id=?", (signal_id,),
            ).fetchone()
            if row is None:
                raise KeyError(signal_id)
            if row["decision"] is not None:
                raise ValueError("Решение уже записано")
            if row["snapshot"] is None:
                raise ValueError("У исторической записи отсутствует снимок доказательств")
            self.db.execute(
                "UPDATE product_queue SET decision=?,reason=?,decided_at=?,decided_by=? WHERE signal_id=?",
                (decision, redact(reason.strip()), utcnow(), actor, signal_id),
            )

    def product_decision(self, signal_id):
        return self.db.execute(
            "SELECT decision,reason,decided_at,decided_by FROM product_queue "
            "WHERE signal_id=? AND decision IS NOT NULL", (signal_id,),
        ).fetchone()

    def audit(self, signal_id):
        return [dict(x) for x in self.db.execute(
            'SELECT actor,action,reason,created_at FROM review_events WHERE signal_id=? ORDER BY id',
            (signal_id,)).fetchall()]

    def evidence_history(self, signal_id):
        if self.db.execute('SELECT 1 FROM signals WHERE id=?', (signal_id,)).fetchone() is None:
            raise KeyError(signal_id)
        rows = self.db.execute(
            'SELECT added_ticket_ids,created_at FROM evidence_events WHERE signal_id=? ORDER BY id',
            (signal_id,)).fetchall()
        return [{'added_ticket_ids':json.loads(row['added_ticket_ids']),
                 'created_at':row['created_at']} for row in rows]

    def close(self):
        self.db.close()
