import tempfile
import unittest
from pathlib import Path
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
        self.store.ingest([SIGNAL])
        self.store.review(SIGNAL['id'], 'approved', 'analyst', 'verified')
        self.assertEqual(self.store.list_signals()[0]['status'], 'approved')
        self.assertEqual(self.store.audit(SIGNAL['id'])[0]['actor'], 'analyst')

    def test_no_repeat_review(self):
        self.store.ingest([SIGNAL])
        self.store.review(SIGNAL['id'], 'rejected', 'analyst', 'not a bug')
        with self.assertRaises(ValueError):
            self.store.review(SIGNAL['id'], 'approved', 'analyst', 'changed mind')

    def test_requires_reason(self):
        self.store.ingest([SIGNAL])
        with self.assertRaises(ValueError):
            self.store.review(SIGNAL['id'], 'approved', 'analyst', '')

    def test_no_unknown_signal(self):
        with self.assertRaises(KeyError):
            self.store.review('missing', 'approved', 'analyst', 'checked')

if __name__ == '__main__':
    unittest.main()
