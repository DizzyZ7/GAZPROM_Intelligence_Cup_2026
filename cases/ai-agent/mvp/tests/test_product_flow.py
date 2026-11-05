import unittest

from evidence import get_signal
from pulse import Ticket
from service import ingest_batch
from store import SignalStore


class ProductFlowTests(unittest.TestCase):
    def setUp(self):
        self.store = SignalStore(":memory:")
        self.addCleanup(self.store.close)

    def test_new_topic_exclusion_and_product_decision(self):
        texts = (
            "Не удаётся выгрузить акт сверки по расчётам",
            "Не удаётся выгрузить акт сверки по расчёту",
            "Не удаётся выгрузить акт сверки по расчётам!",
        )
        tickets = [
            Ticket(str(i), "Кабинет", text, occurred_at=f"2026-11-0{i + 1}T09:00:00+03:00")
            for i, text in enumerate(texts)
        ] + [Ticket("other", "Другой продукт", texts[0])]
        ingest_batch(self.store, tickets)
        self.assertEqual(self.store.list_signals(), [])
        ingest_batch(self.store, tickets, mode="experimental")
        signal, = self.store.list_signals()
        identity = signal["id"]
        self.assertEqual(get_signal(self.store, identity)["ticket_ids"], ["0", "1", "2"])
        self.assertEqual(self.store.queue(), [])
        self.store.exclude_source(identity, "0", "analyst", "Другой период")
        ingest_batch(self.store, tickets, mode="experimental")
        detail = get_signal(self.store, identity)
        self.assertEqual(detail["ticket_ids"], ["1", "2"])
        self.assertEqual(self.store.exclusions(identity)[0]["ticket_id"], "0")
        self.store.review(identity, "approved", "analyst", "Проверены источники")
        row, = self.store.queue()
        self.assertEqual(row["first_ticket_at"], "2026-11-02T06:00:00+00:00")
        self.store.decide(identity, "known", "Есть задача в очереди")
        row, = self.store.queue()
        self.assertEqual(row["decision"], "known")
        self.assertGreaterEqual(row["decided_at"], row["delivered_at"])
        with self.assertRaises(ValueError):
            self.store.decide(identity, "in_progress", "Повторное решение")

    def test_cannot_deliver_after_removing_one_of_two_sources(self):
        tickets = [
            Ticket("1", "Кабинет", "Не приходит СМС для восстановления пароля"),
            Ticket("2", "Кабинет", "Код для восстановления пароля не приходит"),
        ]
        ingest_batch(self.store, tickets)
        signal, = self.store.list_signals()
        identity = signal["id"]
        self.store.exclude_source(identity, "1", "analyst", "Другая проблема")
        with self.assertRaises(ValueError):
            self.store.review(identity, "approved", "analyst", "Один источник")
        self.assertEqual(self.store.queue(), [])
