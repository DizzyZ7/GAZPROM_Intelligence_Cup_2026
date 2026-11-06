import sqlite3
import tempfile
import unittest
from pathlib import Path
from evidence import get_signal
from pulse import Ticket
from store import SignalStore

SIGNAL = {'id':'P:bug:queue','product':'P','category':'bug','workflow':'queue',
          'ticket_count':2,'ticket_ids':['1','2'],'examples':[]}

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SignalStore(Path(self.tmp.name) / 'db.sqlite3')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_import_idempotent(self):
        self.assertEqual(self.store.ingest([SIGNAL]), 1)
        self.assertEqual(self.store.ingest([SIGNAL]), 0)

    def test_review_audit(self):
        self.store.stage_tickets([Ticket("1", "P", "Первое обращение"),
                                  Ticket("2", "P", "Второе обращение")])
        self.store.ingest([SIGNAL])
        revision = get_signal(self.store, SIGNAL['id'])['revision']
        self.store.review(SIGNAL['id'], 'approved', 'analyst', 'verified', expected_revision=revision)
        self.assertEqual(self.store.list_signals()[0]['status'], 'approved')
        self.assertEqual(self.store.audit(SIGNAL['id'])[0]['actor'], 'analyst')

    def test_no_repeat_review(self):
        self.store.ingest([SIGNAL])
        revision = get_signal(self.store, SIGNAL['id'])['revision']
        self.store.review(SIGNAL['id'], 'rejected', 'analyst', 'not a bug', expected_revision=revision)
        with self.assertRaises(ValueError):
            self.store.review(SIGNAL['id'], 'approved', 'analyst', 'changed mind',
                              expected_revision=revision)

    def test_requires_reason(self):
        self.store.ingest([SIGNAL])
        revision = get_signal(self.store, SIGNAL['id'])['revision']
        with self.assertRaises(ValueError):
            self.store.review(SIGNAL['id'], 'approved', 'analyst', '', expected_revision=revision)

    def test_no_unknown_signal(self):
        with self.assertRaises(KeyError):
            self.store.review('missing', 'approved', 'analyst', 'checked', expected_revision='0' * 64)

    def test_legacy_source_lookup_is_scoped_to_product(self):
        self.store.stage_tickets([Ticket("1", "Другой продукт", "Чужой источник")])
        self.store.ingest([SIGNAL])
        card = get_signal(self.store, SIGNAL["id"])
        self.assertEqual(card["sources"][0], {"ticket_id": "1", "excerpt": None, "occurred_at": None})

    def test_legacy_queue_keeps_old_decision_without_inventing_a_snapshot(self):
        path = Path(self.tmp.name) / "legacy.sqlite3"
        db = sqlite3.connect(path)
        try:
            db.executescript("""
                CREATE TABLE product_queue (
                  signal_id TEXT PRIMARY KEY REFERENCES signals(id),
                  first_ticket_at TEXT, delivered_at TEXT NOT NULL,
                  decision TEXT, reason TEXT, decided_at TEXT
                );
                INSERT INTO product_queue VALUES (
                  'P:bug:queue', NULL, '2026-11-01T00:00:00+00:00',
                  'known', 'Есть задача', '2026-11-02T00:00:00+00:00'
                );
            """)
        finally:
            db.close()
        store = SignalStore(path)
        try:
            store.ingest([SIGNAL])
            row, = store.queue()
            self.assertIsNone(row["snapshot"])
            self.assertIsNone(row["decided_by"])
            self.assertEqual(row["decision"], "known")
            self.assertEqual(row["reason"], "Есть задача")
        finally:
            store.close()

    def test_atomic_rollback_discards_staged_tickets_and_signals(self):
        with self.assertRaisesRegex(RuntimeError, "simulated"):
            with self.store.atomic():
                self.store.stage_tickets([Ticket("rollback","P","Очередь ошибка 404")])
                self.store.ingest([SIGNAL])
                raise RuntimeError("simulated")
        self.assertEqual(self.store.staged_tickets(), [])
        self.assertEqual(self.store.list_signals(), [])

    def test_atomic_commit_persists_both(self):
        with self.store.atomic():
            self.store.stage_tickets([Ticket("committed","P","Очередь ошибка 404")])
            self.store.ingest([SIGNAL])
        self.assertEqual(len(self.store.staged_tickets()),1)
        self.assertEqual(len(self.store.list_signals()),1)

    def test_atomic_rejects_nested_transaction(self):
        with self.store.atomic():
            with self.assertRaises(RuntimeError):
                with self.store.atomic():
                    pass

if __name__ == '__main__':
    unittest.main()
