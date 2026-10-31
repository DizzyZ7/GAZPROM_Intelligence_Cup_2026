import tempfile
import unittest
from pathlib import Path
from store import SignalStore

class GrowthTests(unittest.TestCase):
    def test_growth_and_review(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SignalStore(Path(directory) / "pulse.sqlite3")
            signal = {"id":"p:feature_request:export","product":"p",
                      "category":"feature_request","workflow":"export",
                      "ticket_count":2,"ticket_ids":["1","2"],"examples":["Экспорт"]}
            try:
                self.assertEqual(store.ingest([signal]), 1)
                store.review(signal["id"],"approved","analyst","Проверено")
                next_batch = dict(signal, ticket_ids=["2","3"])
                self.assertEqual(store.ingest([next_batch]), 0)
                self.assertEqual(store.list_signals()[0]["ticket_count"], 3)
                self.assertEqual(store.list_signals()[0]["status"], "approved")
                self.assertEqual(len(store.audit(signal["id"])), 1)
                self.assertEqual(store.ingest([next_batch]), 0)
                self.assertEqual(store.list_signals()[0]["ticket_count"], 3)
            finally:
                store.close()

if __name__ == "__main__":
    unittest.main()
