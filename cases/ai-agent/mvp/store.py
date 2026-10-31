"""SQLite review queue for offline demo; not a production authorization layer."""
import json
import sqlite3
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
        self.db.commit()

    def ingest(self, signals):
        """Идемпотентный импорт с накоплением новых источников.

        Существующие решения аналитика не сбрасываются.
        """
        inserted = 0
        with self.db:
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
                    inserted += 1
                    continue
                if (current['product'],current['category'],current['workflow']) != (
                    s['product'],s['category'],s['workflow']):
                    raise ValueError('Конфликт идентификатора сигнала')
                old = json.loads(current['payload'])
                merged_ids = list(dict.fromkeys(old.get('ticket_ids', []) + incoming_ids))
                if len(merged_ids) == len(old.get('ticket_ids', [])):
                    continue
                old['ticket_ids'] = merged_ids
                old['ticket_count'] = len(merged_ids)
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
        with self.db:
            current = self.db.execute('SELECT status FROM signals WHERE id=?', (signal_id,)).fetchone()
            if current is None:
                raise KeyError(signal_id)
            if current['status'] != 'pending_human_review':
                raise ValueError('Already reviewed; no implicit override')
            self.db.execute('UPDATE signals SET status=?,updated_at=? WHERE id=?',
                            (action, utcnow(), signal_id))
            self.db.execute('INSERT INTO review_events(signal_id,actor,action,reason,created_at) VALUES(?,?,?,?,?)',
                            (signal_id, actor, action, reason, utcnow()))

    def audit(self, signal_id):
        return [dict(x) for x in self.db.execute(
            'SELECT actor,action,reason,created_at FROM review_events WHERE signal_id=? ORDER BY id',
            (signal_id,)).fetchall()]

    def close(self):
        self.db.close()
