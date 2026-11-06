import json
import tempfile
import unittest
from pathlib import Path
from evidence import get_signal
from pulse import Ticket
from service import ExactTokenProvider, import_tickets, ingest_batch
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
        store.review(signal_id,"approved","analyst","verified",
                     expected_revision=get_signal(store, signal_id)["revision"])
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

    def test_signal_emerges_across_files(self):
        for identifier in ("first", "second"):
            self.file.write_text(json.dumps([
                {"id":identifier,"product":"P","text":"Очередь ошибка 404"}
            ]), encoding="utf-8")
            result = import_tickets(self.db, self.file)
        self.assertEqual(result["new_signals"], 1)
        with_store = SignalStore(self.db)
        try:
            self.assertEqual(len(with_store.staged_tickets()), 2)
            self.assertEqual(with_store.list_signals()[0]["ticket_count"], 2)
        finally:
            with_store.close()

    def test_conflicting_file_rolls_back_entire_batch(self):
        import_tickets(self.db, self.file)
        self.file.write_text(json.dumps([
            {"id":"new","product":"P","text":"Очередь ошибка 404"},
            {"id":"1","product":"Q","text":"Очередь ошибка 404"}
        ]), encoding="utf-8")
        with self.assertRaises(ValueError):
            import_tickets(self.db, self.file)
        store = SignalStore(self.db)
        try:
            self.assertEqual({t.id for t in store.staged_tickets()}, {"1", "2"})
            self.assertEqual(len(store.evidence_history("P:bug:booking_queue")), 1)
        finally:
            store.close()

    def test_provider_failure_rolls_back_staging(self):
        class BrokenProvider:
            def embed(self, texts):
                raise RuntimeError("Модель недоступна")
        with self.assertRaises(RuntimeError):
            import_tickets(self.db, self.file, mode="semantic", provider=BrokenProvider())
        store = SignalStore(self.db)
        try:
            self.assertEqual(store.staged_tickets(), [])
            self.assertEqual(store.list_signals(), [])
        finally:
            store.close()

    def test_rules_cannot_merge_two_existing_semantic_signals(self):
        store = SignalStore(self.db)
        try:
            store.stage_tickets([Ticket(str(i), "P", "Очередь ошибка 404") for i in range(1, 6)])
            for identity, members, action in (
                ("semantic-first", ["1", "2", "3"], "approved"),
                ("semantic-second", ["4", "5"], "rejected"),
            ):
                store.ingest([{"id":identity,"product":"P","category":"bug",
                               "workflow":"booking_queue", "ticket_count":len(members),
                               "ticket_ids":members,"examples":[]}])
                store.review(identity, action, "analyst", "Проверено по источникам",
                             expected_revision=get_signal(store, identity)["revision"])
        finally:
            store.close()
        self.file.write_text(json.dumps([
            {"id":"6","product":"P","text":"Очередь ошибка 404"}
        ]), encoding="utf-8")
        with self.assertRaises(ValueError):
            import_tickets(self.db, self.file)
        store = SignalStore(self.db)
        try:
            self.assertEqual(len(store.staged_tickets()), 5)
            self.assertEqual(len(store.list_signals()), 2)
            self.assertEqual([s["status"] for s in store.list_signals()], ["approved", "rejected"])
        finally:
            store.close()

    def test_semantic_replay_does_not_recreate_excluded_only_cluster(self):
        booking_error = "Бронирование очереди ошибка 404 подтверждение слота"
        tickets = [
            Ticket("a", "P", "Бронирование очереди ошибка 404 старый талон"),
            Ticket("b", "P", booking_error),
            Ticket("c", "P", booking_error),
        ]
        store = SignalStore(self.db)
        try:
            ingest_batch(store, tickets)
            provider = ExactTokenProvider()
            before = ingest_batch(store, tickets, mode="semantic", provider=provider)
            self.assertEqual(before["signals_found"], 1)
            self.assertEqual(before["new_signals"], 0)
            original, = store.list_signals()
            signal_id = original["id"]
            for ticket_id in ("b", "c"):
                card = get_signal(store, signal_id)
                store.exclude_source(signal_id, ticket_id, "analyst", "Другой сбой",
                                     expected_revision=card["revision"])

            replay = ingest_batch(store, tickets, mode="semantic", provider=provider)
            self.assertEqual(replay["staged_new"], 0)
            self.assertEqual(replay["new_signals"], 0)
            self.assertEqual([row["id"] for row in store.list_signals()], [signal_id])
            self.assertEqual(get_signal(store, signal_id)["ticket_ids"], ["a"])

            later = ingest_batch(store, [Ticket("d", "P", booking_error)],
                                 mode="semantic", provider=provider)
            self.assertEqual(later["new_signals"], 1)
            independent, = [row for row in store.list_signals() if row["id"] != signal_id]
            self.assertEqual(get_signal(store, independent["id"])["ticket_ids"], ["b", "c", "d"])
            self.assertEqual(get_signal(store, signal_id)["ticket_ids"], ["a"])
        finally:
            store.close()
if __name__=="__main__":
    unittest.main()
