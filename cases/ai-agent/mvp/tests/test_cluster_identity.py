import tempfile
import unittest
from pathlib import Path
from store import SignalStore
from cluster_identity import reconcile
from evidence import get_signal
from pulse import Ticket

def signal(identifier, ids, product="P"):
    return {"id":identifier,"product":product,"category":"feature_request",
            "workflow":"export","ticket_count":len(ids),
            "ticket_ids":ids,"examples":[]}

class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=SignalStore(Path(self.tmp.name)/"test.sqlite3")
    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
    def test_reuses_identity(self):
        self.store.ingest([signal("original",["1","2"])])
        self.assertEqual(reconcile(self.store,[signal("new",["2","3"])])[0]["id"],"original")
    def test_no_cross_product(self):
        self.store.ingest([signal("original",["1","2"])])
        self.assertEqual(reconcile(self.store,[signal("new",["2","3"],"Q")])[0]["id"],"new")
    def test_no_overlap(self):
        self.store.ingest([signal("original",["1","2"])])
        self.assertEqual(reconcile(self.store,[signal("new",["3","4"])])[0]["id"],"new")
    def test_ambiguous_overlap(self):
        self.store.ingest([signal("first",["1","2"]),signal("second",["3","4"])])
        with self.assertRaises(ValueError):
            reconcile(self.store,[signal("new",["2","3"])])
    def test_one_old_to_two_new(self):
        self.store.ingest([signal("original",["1","2"])])
        with self.assertRaises(ValueError):
            reconcile(self.store,[signal("new1",["1","3"]),signal("new2",["2","4"])])

    def test_unequal_overlap_cannot_merge_reviewed_signals(self):
        self.store.stage_tickets([Ticket(str(i), "P", "Пожелание по экспорту") for i in range(1, 5)])
        self.store.ingest([signal("first",["1","2"]),signal("second",["3","4"])])
        self.store.review("first", "approved", "analyst", "Проверено",
                          expected_revision=get_signal(self.store, "first")["revision"])
        self.store.review("second", "rejected", "analyst", "Другая причина",
                          expected_revision=get_signal(self.store, "second")["revision"])
        with self.assertRaises(ValueError):
            reconcile(self.store,[signal("new",["1","2","3"])])
        self.assertEqual([row["status"] for row in self.store.list_signals()],
                         ["approved", "rejected"])

if __name__=="__main__":
    unittest.main()
