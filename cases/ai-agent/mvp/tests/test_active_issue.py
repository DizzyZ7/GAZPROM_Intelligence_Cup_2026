"""Регрессии рабочего импорта и проверки актуальности доказательств."""

import importlib
import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from evidence import get_signal
from pipeline import analyze_semantic
from pulse import Ticket
from service import ingest_batch
from store import SignalStore

ACTIVE = "При бронировании очереди ошибка 404, запись не создается."
RESOLVED = (
    "При бронировании очереди была ошибка 404. "
    "Сбой исправили, теперь бронирование работает."
)


class RecordingProvider:
    def __init__(self):
        self.seen = []

    def embed(self, texts):
        self.seen.extend(texts)
        return [[1.0, 0.0] for _ in texts]


class ActiveIssueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "pulse.db")

    def test_resolved_source_cannot_turn_single_complaint_into_signal(self):
        with closing(SignalStore(self.db)) as store:
            result = ingest_batch(
                store,
                [Ticket("active", "P", ACTIVE), Ticket("resolved", "P", RESOLVED)],
            )
            self.assertEqual(result["signals_found"], 0)
            self.assertEqual(store.list_signals(), [])
            self.assertEqual(len(store.staged_tickets()), 2)
            self.assertEqual(result["analyzed_total"], 2)
            self.assertEqual(result["resolved_tickets"], 1)

    def test_negation_promises_and_recurrent_failure_remain_actionable(self):
        texts = [
            ACTIVE,
            "Очередь ошибка 404. Сбой не исправили.",
            "Очередь ошибка 404. Сбой еще не исправлен.",
            "Очередь ошибка 404. Сбой пока не устранен.",
            "Очередь ошибка 404. Сбой не был исправлен.",
            "Очередь ошибка 404. Сбой будет исправлен завтра.",
            "Очередь ошибка 404. Обещали, что сбой исправят.",
            "Очередь ошибка 404. Если сбой исправили, почему ошибка осталась?",
            "Очередь ошибка 404. Хотелось бы, чтобы сбой исправили.",
            "Очередь ошибка 404. Сбой исправили, но ошибка все еще возникает.",
            "Очередь ошибка 404. Сбой исправили, но бронирование не работает.",
            "Очередь ошибка 404. Сбой устранили вчера. Сегодня снова ошибка 404.",
            "Очередь ошибка 404. В поддержке говорят, что сбой исправили, но это не так.",
            "Очередь ошибка 404. Сбой исправили?",
            "Очередь ошибка 404. Вы исправили сбой?",
            "Очередь ошибка 404. Сбой исправили бы поскорее.",
            "Очередь ошибка 404. Сбой исправили вчера. Сегодня при бронировании ошибка 500.",
            "Очередь ошибка 404. Сбой исправили, если верить поддержке.",
            "Очередь ошибка 404. Сбой исправили, но записи не создаются.",
            "Очередь ошибка 404. Сбой исправлен не был.",
            "Очередь ошибка 404. Сбой исправлен, только если очистить кеш.",
            "Очередь ошибка 404. Сбой исправили — обещают в поддержке.",
            "Очередь ошибка 404. По словам поддержки, сбой исправили.",
            "Очередь ошибка 404. Поддержка сообщила: сбой исправлен.",
            "Очередь ошибка 404. Хочу, чтоб сбой исправили.",
            "Очередь ошибка 404. Надеюсь, сбой исправили.",
            "Очередь ошибка 404. Подскажите, сбой исправили.",
            "Очередь ошибка 404. Сбой исправили или нет.",
            "Очередь ошибка 404. Если\nсбой исправили.",
            "Очередь ошибка 404. Не считаю, что\nсбой исправили.",
            "Очередь ошибка 404. Поддержка сообщила:\nСбой исправлен.",
            "Очередь ошибка 404. Хочу, чтоб\nсбой исправили.",
        ]
        with closing(SignalStore(self.db)) as store:
            tickets = [Ticket(str(i), "P", text) for i, text in enumerate(texts)]
            ingest_batch(store, tickets)
            (signal,) = store.list_signals()
            self.assertEqual(
                get_signal(store, signal["id"])["ticket_count"], len(texts)
            )

    def test_explicit_resolution_forms_are_not_clustered(self):
        texts = [
            RESOLVED,
            "Очередь ошибка 404. Ошибка уже исправлена, спасибо.",
            "Очередь ошибка 404. Проблему устранили, все работает.",
            "Очередь ошибка 404. Исправили сбой, заявка закрыта.",
            "Очередь ошибка 404. Ошибка больше не возникает.",
            "Очередь ошибка 404. Сбой был устранен.",
        ]
        with closing(SignalStore(self.db)) as store:
            result = ingest_batch(
                store, [Ticket(str(i), "P", text) for i, text in enumerate(texts)]
            )
            self.assertEqual(store.list_signals(), [])
            self.assertEqual(result["resolved_tickets"], len(texts))

    def test_semantic_mode_filters_resolution_before_embedding(self):
        provider = RecordingProvider()
        tickets = [
            Ticket("a", "P", ACTIVE),
            Ticket("b", "P", ACTIVE),
            Ticket("resolved", "P", RESOLVED),
        ]
        result = analyze_semantic(tickets, provider)
        (signal,) = result["signals"]
        self.assertEqual(signal["ticket_ids"], ["a", "b"])
        self.assertEqual(provider.seen, [ACTIVE, ACTIVE])
        self.assertEqual(result["processed"], 3)

    def test_resolved_reimport_does_not_change_approved_snapshot(self):
        with closing(SignalStore(self.db)) as store:
            initial = [Ticket("a", "P", ACTIVE), Ticket("b", "P", ACTIVE)]
            ingest_batch(store, initial)
            (signal,) = store.list_signals()
            identity = signal["id"]
            store.review(
                identity,
                "approved",
                "analyst",
                "Проверены действующие ошибки",
                expected_revision=get_signal(store, identity)["revision"],
            )
            approved = get_signal(store, identity)
            queue = store.queue()
            for _ in range(2):
                ingest_batch(store, [Ticket("resolved", "P", RESOLVED)])
                self.assertEqual(get_signal(store, identity), approved)
                self.assertEqual(store.queue(), queue)
                self.assertEqual(len(store.audit(identity)), 1)

    def test_old_card_requires_exclusion_of_resolved_evidence(self):
        # Старые карточки могли сохранить устраненный сбой до появления проверки.
        # Статус расположен за пределами показываемого фрагмента в 300 символов.
        with closing(SignalStore(self.db)) as store:
            tickets = [
                Ticket("a", "P", ACTIVE),
                Ticket("b", "P", ACTIVE),
                Ticket("resolved", "P", ACTIVE + " " + "Подробности. " * 35 + RESOLVED),
            ]
            store.stage_tickets(tickets)
            store.ingest(
                [
                    {
                        "id": "legacy",
                        "product": "P",
                        "category": "bug",
                        "workflow": "booking_queue",
                        "ticket_count": 3,
                        "ticket_ids": [ticket.id for ticket in tickets],
                        "examples": [],
                    }
                ]
            )
            card = get_signal(store, "legacy")
            with self.assertRaisesRegex(ValueError, "устранени"):
                store.review(
                    "legacy",
                    "approved",
                    "analyst",
                    "Проверено",
                    expected_revision=card["revision"],
                )
            self.assertEqual(store.audit("legacy"), [])
            self.assertEqual(store.queue(), [])
            self.assertEqual(get_signal(store, "legacy"), card)
            self.assertEqual(card["sources"][2]["analysis_warning"], "resolved_issue")
            store.exclude_source(
                "legacy",
                "resolved",
                "analyst",
                "Этот сбой уже устранен",
                expected_revision=card["revision"],
            )
            store.review(
                "legacy",
                "approved",
                "analyst",
                "Остались два действующих сбоя",
                expected_revision=get_signal(store, "legacy")["revision"],
            )
            self.assertEqual(store.queue()[0]["snapshot"]["ticket_ids"], ["a", "b"])

    def test_semantic_legacy_growth_explains_conflict_and_rolls_back(self):
        with closing(SignalStore(self.db)) as store:
            initial = [
                Ticket("a", "P", ACTIVE),
                Ticket("b", "P", ACTIVE),
                Ticket("resolved", "P", RESOLVED),
            ]
            store.stage_tickets(initial)
            store.ingest(
                [
                    {
                        "id": "legacy",
                        "product": "P",
                        "category": "bug",
                        "workflow": "booking_queue",
                        "ticket_count": 3,
                        "ticket_ids": [ticket.id for ticket in initial],
                        "examples": [],
                        "method": "complete_link_embeddings",
                        "threshold": 0.86,
                    }
                ]
            )
            card = get_signal(store, "legacy")
            history = store.evidence_history("legacy")
            later = [Ticket("c", "P", ACTIVE)]
            with self.assertRaisesRegex(ValueError, "проверка аналитиком"):
                ingest_batch(
                    store, later, mode="semantic", provider=RecordingProvider()
                )
            self.assertEqual(store.staged_tickets(), initial)
            self.assertEqual(get_signal(store, "legacy"), card)
            self.assertEqual(store.evidence_history("legacy"), history)
            store.exclude_source(
                "legacy",
                "resolved",
                "analyst",
                "Сбой устранен",
                expected_revision=card["revision"],
            )
            ingest_batch(store, later, mode="semantic", provider=RecordingProvider())
            self.assertEqual(get_signal(store, "legacy")["ticket_ids"], ["a", "b", "c"])


class ActiveIssueAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import fastapi  # noqa: F401
            import httpx  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("Для API-тестов установите requirements-api.txt")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(
            os.environ,
            {
                "PULSE_DB": str(Path(self.tmp.name) / "api.db"),
                "PULSE_REVIEW_TOKEN": "test-local-token",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        from fastapi.testclient import TestClient

        import api

        self.client = TestClient(importlib.reload(api).app)
        self.addCleanup(self.client.close)
        self.headers = {"X-Review-Token": "test-local-token"}

    def test_import_and_review_use_only_current_complaints(self):
        def post_tickets(rows):
            response = self.client.post(
                "/imports",
                headers=self.headers,
                json={
                    "tickets": [{"id": i, "product": "P", "text": t} for i, t in rows]
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()

        first = post_tickets([("a", ACTIVE), ("resolved", RESOLVED)])
        self.assertEqual(first["signals_found"], 0)
        self.assertEqual(first["resolved_tickets"], 1)
        second = post_tickets([("b", ACTIVE)])
        self.assertEqual(second["new_signals"], 1)
        url = "/signals/P:bug:booking_queue"
        card = self.client.get(url, headers=self.headers).json()
        self.assertEqual(card["ticket_ids"], ["a", "b"])
        decision = self.client.post(
            url + "/review",
            headers=self.headers,
            json={
                "action": "approved",
                "reason": "Проверены два активных обращения",
                "expected_revision": card["revision"],
            },
        )
        self.assertEqual(decision.status_code, 200, decision.text)
        queue = self.client.get("/product-queue", headers=self.headers).json()
        self.assertEqual(queue[0]["snapshot"]["ticket_ids"], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
