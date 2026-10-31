import tempfile
import unittest
from pathlib import Path
from pulse import Ticket
from store import SignalStore

class StagingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=SignalStore(Path(self.temp.name)/"staging.db")
    def tearDown(self):
        self.store.close()
        self.temp.cleanup()
    def test_idempotent_staging(self):
        tickets=[Ticket("1","P","Очередь ошибка 404")]
        self.assertEqual(self.store.stage_tickets(tickets),1)
        self.assertEqual(self.store.stage_tickets(tickets),0)
        self.assertEqual(len(self.store.staged_tickets()),1)
    def test_redacted_before_storage(self):
        self.store.stage_tickets([Ticket("1","P","Пишите на demo@example.org")])
        row=self.store.db.execute("SELECT text FROM staged_tickets").fetchone()
        self.assertNotIn("demo@example.org",row["text"])
        self.assertIn("[EMAIL]",row["text"])
    def test_conflicting_id_does_not_overwrite(self):
        self.store.stage_tickets([Ticket("1","P","Первое обращение")])
        with self.assertRaises(ValueError):
            self.store.stage_tickets([Ticket("1","Q","Другое обращение")])
        self.assertEqual(self.store.staged_tickets()[0].product,"P")

if __name__=="__main__":
    unittest.main()
