"""SQLite review queue for offline demo; not a production authorization layer."""
import json
import sqlite3
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path

STATES = {'pending_human_review', 'approved', 'rejected'}

def utcnow():
    return datetime.now(timezone.utc).isoformat()

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
        ''')
        self.db.execute("""CREATE TABLE IF NOT EXISTS evidence_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          signal_id TEXT NOT NULL,
          added_ticket_ids TEXT NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY(signal_id) REFERENCES signals(id)
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS staged_tickets (
          id TEXT PRIMARY KEY, product TEXT NOT NULL, text TEXT NOT NULL,
          channel TEXT NOT NULL, created_at TEXT NOT NULL
        )""")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS ticket_dates (
          ticket_id TEXT PRIMARY KEY REFERENCES staged_tickets(id),
          occurred_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS excluded_sources (
          signal_id TEXT NOT NULL REFERENCES signals(id),
          ticket_id TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
          created_at TEXT NOT NULL, PRIMARY KEY(signal_id,ticket_id)
        );
        CREATE TABLE IF NOT EXISTS product_queue (
          signal_id TEXT PRIMARY KEY REFERENCES signals(id),
          first_ticket_at TEXT, delivered_at TEXT NOT NULL,
          decision TEXT, reason TEXT, decided_at TEXT
        );
        """)
        self.db.commit()

    @contextmanager
    def atomic(self):
        """Единая транзакция для staging, анализа и сохранения сигналов."""
        if self.db.in_transaction:
            raise RuntimeError("Нельзя открыть вложенную транзакцию импорта")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def stage_tickets(self, tickets):
        """Идемпотентное сохранение обезличенных обращений."""
        from pulse import redact
        inserted = 0
        with (nullcontext() if self.db.in_transaction else self.db):
            for ticket in tickets:
                sanitized = redact(ticket.text)
                occurred_at = None
                if ticket.occurred_at is not None:
                    try:
                        date = datetime.fromisoformat(ticket.occurred_at.replace("Z", "+00:00"))
                        if date.utcoffset() is None:
                            raise ValueError()
                        occurred_at = date.astimezone(timezone.utc).isoformat()
                    except (ValueError, AttributeError, OverflowError) as exc:
                        raise ValueError("Дата обращения должна содержать часовой пояс") from exc
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
                if occurred_at:
                    old_date = self.db.execute(
                        'SELECT occurred_at FROM ticket_dates WHERE ticket_id=?', (ticket.id,)
                    ).fetchone()
                    if old_date and old_date[0] != occurred_at:
                        raise ValueError("Дата обращения не совпадает с сохранённой")
                    self.db.execute(
                        'INSERT OR IGNORE INTO ticket_dates VALUES (?,?)', (ticket.id, occurred_at)
                    )
        return inserted

    def staged_tickets(self):
        from pulse import Ticket
        return [Ticket(row['id'],row['product'],row['text'],row['channel'])
                for row in self.db.execute(
                    'SELECT id,product,text,channel FROM staged_tickets ORDER BY id')]

    def ingest(self, signals):
        """Идемпотентный импорт с накоплением новых источников.

        Существующие решения аналитика не сбрасываются.
        """
        inserted = 0
        with (nullcontext() if self.db.in_transaction else self.db):
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
                    self.db.execute(
                        'INSERT INTO evidence_events(signal_id,added_ticket_ids,created_at) VALUES(?,?,?)',
                        (s['id'],json.dumps(incoming_ids,ensure_ascii=False),utcnow()))
                    inserted += 1
                    continue
                if (current['product'],current['category'],current['workflow']) != (
                    s['product'],s['category'],s['workflow']):
                    raise ValueError('Конфликт идентификатора сигнала')
                old = json.loads(current['payload'])
                excluded = {r[0] for r in self.db.execute(
                    'SELECT ticket_id FROM excluded_sources WHERE signal_id=?', (s['id'],))}
                incoming_ids = [i for i in incoming_ids if i not in excluded]
                merged_ids = list(dict.fromkeys(old.get('ticket_ids', []) + incoming_ids))
                if len(merged_ids) == len(old.get('ticket_ids', [])):
                    continue
                additions = [item for item in merged_ids if item not in old.get('ticket_ids', [])]
                self.db.execute(
                    'INSERT INTO evidence_events(signal_id,added_ticket_ids,created_at) VALUES(?,?,?)',
                    (s['id'],json.dumps(additions,ensure_ascii=False),utcnow()))
                old['ticket_ids'] = merged_ids
                old['ticket_count'] = len(merged_ids)
                if excluded:
                    old['examples'] = [r['excerpt'] for r in self.sources(merged_ids) if r['excerpt']][:2]
                else:
                    old['examples'] = list(dict.fromkeys(
                        old.get('examples', []) + s.get('examples', [])))[:2]
                self.db.execute(
                    'UPDATE signals SET ticket_count=?,payload=?,updated_at=? WHERE id=?',
                    (len(merged_ids),json.dumps(old,ensure_ascii=False),utcnow(),s['id']))
        return inserted

    def list_signals(self, status=None):
        if status is not None and status not in STATES:
            raise ValueError('Unknown status')
        sql = 'SELECT id,product,category,workflow,ticket_count,status FROM signals'
        rows = self.db.execute(sql + (' WHERE status=?' if status else '') + ' ORDER BY id',
                               (status,) if status else ()).fetchall()
        return [dict(r) for r in rows]

    def review(self, signal_id, action, actor, reason):
        if action not in {'approved', 'rejected'}:
            raise ValueError('Invalid review action')
        if not actor.strip() or not reason.strip():
            raise ValueError('Reviewer and reason required')
        with self.atomic():
            current = self.db.execute('SELECT status,ticket_count,payload FROM signals WHERE id=?', (signal_id,)).fetchone()
            if current is None:
                raise KeyError(signal_id)
            if current['status'] != 'pending_human_review':
                raise ValueError('Already reviewed; no implicit override')
            if action == 'approved' and current['ticket_count'] < 2:
                raise ValueError('Для подтверждения нужны хотя бы два обращения')
            now = utcnow()
            self.db.execute('UPDATE signals SET status=?,updated_at=? WHERE id=?',
                            (action, now, signal_id))
            self.db.execute('INSERT INTO review_events(signal_id,actor,action,reason,created_at) VALUES(?,?,?,?,?)',
                            (signal_id, actor, action, reason, now))
            if action == 'approved':
                dates = [r['occurred_at'] for r in self.sources(json.loads(current['payload'])['ticket_ids'])]
                first = min(dates) if dates and all(dates) else None
                self.db.execute(
                    'INSERT INTO product_queue(signal_id,first_ticket_at,delivered_at) VALUES (?,?,?)',
                    (signal_id, first, now))

    def sources(self, ids):
        rows = []
        for ticket_id in ids:
            row = self.db.execute(
                'SELECT text,occurred_at FROM staged_tickets LEFT JOIN ticket_dates '
                'ON ticket_id=id WHERE id=?', (ticket_id,)).fetchone()
            rows.append({
                'ticket_id': ticket_id,
                'excerpt': row['text'][:300] if row else None,
                'occurred_at': row['occurred_at'] if row else None,
            })
        return rows

    def exclude_source(self, signal_id, ticket_id, actor, reason):
        from pulse import redact
        if not reason.strip():
            raise ValueError('Укажите основание исключения')
        with self.atomic():
            row = self.db.execute('SELECT status,payload FROM signals WHERE id=?', (signal_id,)).fetchone()
            if row is None:
                raise KeyError(signal_id)
            payload = json.loads(row['payload'])
            ids = payload['ticket_ids']
            if row['status'] != 'pending_human_review' or ticket_id not in ids or len(ids) < 2:
                raise ValueError('Источник можно исключить только из открытой карточки с двумя или более обращениями')
            ids.remove(ticket_id)
            now = utcnow()
            payload['ticket_count'] = len(ids)
            payload['examples'] = [r['excerpt'] for r in self.sources(ids) if r['excerpt']][:2]
            self.db.execute('INSERT INTO excluded_sources VALUES (?,?,?,?,?)',
                            (signal_id, ticket_id, actor, redact(reason), now))
            self.db.execute('UPDATE signals SET ticket_count=?,payload=?,updated_at=? WHERE id=?',
                            (len(ids), json.dumps(payload, ensure_ascii=False), now, signal_id))

    def exclusions(self, signal_id):
        return [dict(row) for row in self.db.execute(
            'SELECT ticket_id,actor,reason,created_at FROM excluded_sources WHERE signal_id=? ORDER BY created_at',
            (signal_id,))]

    def queue(self):
        return [dict(row) for row in self.db.execute(
            'SELECT q.*,s.product,s.workflow FROM product_queue q JOIN signals s ON s.id=q.signal_id '
            'ORDER BY q.delivered_at DESC,q.signal_id')]

    def decide(self, signal_id, decision, reason):
        from pulse import redact
        if decision not in {'in_progress', 'known', 'needs_clarification'} or not reason.strip():
            raise ValueError('Укажите допустимое решение и основание')
        with self.atomic():
            row = self.db.execute('SELECT decision FROM product_queue WHERE signal_id=?', (signal_id,)).fetchone()
            if row is None:
                raise KeyError(signal_id)
            if row['decision'] is not None:
                raise ValueError('Решение уже записано')
            self.db.execute(
                'UPDATE product_queue SET decision=?,reason=?,decided_at=? WHERE signal_id=?',
                (decision, redact(reason), utcnow(), signal_id))

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
