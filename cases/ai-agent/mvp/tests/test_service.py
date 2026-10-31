import json
import tempfile
import unittest
from pathlib import Path
from service import import_tickets
from store import SignalStore

class SameVector:
    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]

class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name)/"test.db")
        self.file = Path(self.tmp.name)/"tickets.json"
        self.file.write_text(json.dumps([
            {"id":"1","product":"P","text":"Очередь ошибка 404"},
            {"id":"2","product":"P","text":"Бронирование очереди ошибка 404"}
        ]),encoding="utf-8")
    def tearDown(self):
        self.tmp.cleanup()
    def test_rules_import(self):
        self.assertEqual(import_tickets(self.db,self.file)["new_signals"],1)
    def test_repeat_is_idempotent(self):
        import_tickets(self.db,self.file)
        self.assertEqual(import_tickets(self.db,self.file)["new_signals"],0)
    def test_semantic_import(self):
        result=import_tickets(self.db,self.file,mode="semantic",provider=SameVector())
        self.assertEqual(result["new_signals"],1)
        self.assertEqual(result["mode"],"semantic")
    def test_review_is_preserved(self):
        import_tickets(self.db,self.file)
        store=SignalStore(self.db)
        signal_id=store.list_signals()[0]["id"]
        store.review(signal_id,"approved","analyst","verified")
        store.close()
        import_tickets(self.db,self.file)
        store=SignalStore(self.db)
        self.assertEqual(store.list_signals()[0]["status"],"approved")
        store.close()
    def test_semantic_requires_provider(self):
        with self.assertRaises(ValueError):
            import_tickets(self.db,self.file,mode="semantic")
    def test_reject_nonlist(self):
        self.file.write_text('{"bad":1}',encoding="utf-8")
        with self.assertRaises(ValueError):
            import_tickets(self.db,self.file)
if __name__=="__main__":
    unittest.main()
