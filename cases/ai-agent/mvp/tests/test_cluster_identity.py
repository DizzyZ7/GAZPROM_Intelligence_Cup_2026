import tempfile
import unittest
from pathlib import Path
from store import SignalStore
from cluster_identity import reconcile

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
        self.store.ingest([signal("first",["1","2"]),signal("second",["3","4"])])
        self.store.review("first", "approved", "analyst", "Проверено")
        self.store.review("second", "rejected", "analyst", "Другая причина")
        with self.assertRaises(ValueError):
            reconcile(self.store,[signal("new",["1","2","3"])])
        self.assertEqual([row["status"] for row in self.store.list_signals()],
                         ["approved", "rejected"])

    def test_exact_migration_persists_context_and_preserves_decision(self):
        original = signal("original", ["1", "2"])
        self.store.ingest([original])
        self.store.review("original", "approved", "analyst", "Проверено")
        contextual = dict(signal("new", ["2", "1"]), analysis_version="context-v1",
                          context={"operation": "report_export", "symptom": "schedule"})
        self.store.ingest(reconcile(self.store, [contextual]))
        from evidence import get_signal
        detail = get_signal(self.store, "original")
        self.assertEqual(detail["context"], contextual["context"])
        self.assertEqual(detail["status"], "approved")
        self.assertEqual(len(self.store.evidence_history("original")), 1)
        self.assertEqual(reconcile(self.store, [original])[0]["id"], "original")
        with self.assertRaises(ValueError):
            reconcile(self.store, [signal("original", ["1", "2", "3"])])

    def test_context_cannot_reinterpret_old_sources_as_another_workflow(self):
        self.store.ingest([signal("original", ["1", "2"])])
        contextual = dict(signal("new", ["1", "2"]), workflow="other_workflow",
                          analysis_version="context-v1", context={"operation": "export", "symptom": "schedule"})
        with self.assertRaises(ValueError):
            reconcile(self.store, [contextual])

    def test_context_cannot_extend_embedding_cluster_without_its_checks(self):
        def contextual(identifier, ids, method):
            return dict(signal(identifier, ids), method=method, analysis_version="context-v1",
                        context={"operation": "export", "symptom": "schedule"})

        self.store.ingest([contextual("original", ["1", "2"], "complete_link_embeddings")])
        with self.assertRaises(ValueError):
            reconcile(self.store, [contextual("new", ["1", "2", "3"], "context_rules")])

if __name__=="__main__":
    unittest.main()
