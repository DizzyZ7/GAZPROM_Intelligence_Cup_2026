import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier
from urllib.parse import quote
from unittest.mock import patch


DEMO_FILE = Path(__file__).resolve().parents[1] / "demo_product_flow.json"
HEADERS = {"X-Review-Token": "test-review-token"}


class ReviewHandoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from fastapi.testclient import TestClient
        except (ImportError, RuntimeError) as exc:
            raise unittest.SkipTest("Для API-тестов установите requirements-api.txt") from exc
        cls.client_class = TestClient

    def setUp(self):
        import api

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "pulse.db"
        config = patch.multiple(api, DB_PATH=str(self.db), TOKEN=HEADERS["X-Review-Token"])
        config.start()
        self.addCleanup(config.stop)
        self.client = self.client_class(api.app, headers=HEADERS)
        self.addCleanup(self.client.close)
        self.tickets = json.loads(DEMO_FILE.read_text(encoding="utf-8"))
        self.url = "/signals/Кабинет:bug:booking_queue"
        self.queue_url = "/product-queue"
        self.decision_url = self.queue_url + "/Кабинет:bug:booking_queue/decision"

    def import_tickets(self, tickets=None, status=200):
        response = self.client.post("/imports", json={
            "tickets": self.tickets if tickets is None else tickets,
        })
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def detail(self):
        response = self.client.get(self.url + "/card")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def submit_review(self, card, action="approved"):
        return self.client.post(self.url + "/review", json={
            "action": action, "reason": "Проверено по обращениям",
            "expected_revision": card["revision"],
        })

    def exclude(self, card, ticket_id):
        return self.client.post(self.url + "/exclude", json={
            "ticket_id": ticket_id, "reason": "Другая операция, история талонов",
            "expected_revision": card["revision"],
        })

    def queue(self):
        response = self.client.get(self.queue_url)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_review_requires_revision(self):
        self.import_tickets()
        response = self.client.post(self.url + "/review", json={
            "action": "approved", "reason": "Проверено",
        })
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.detail()["status"], "pending_human_review")

    def test_growth_invalidates_review_and_exclusion(self):
        self.import_tickets(self.tickets[:2])
        seen = self.detail()
        self.import_tickets(self.tickets[2:])
        for response in (self.submit_review(seen), self.exclude(seen, "booking-1")):
            self.assertEqual(response.status_code, 409, response.text)
        current = self.detail()
        self.assertEqual(current["status"], "pending_human_review")
        self.assertNotEqual(seen["revision"], current["revision"])
        self.assertEqual(current["ticket_count"], 3)
        self.assertEqual(self.queue(), [])

    def test_reinstated_source_list_does_not_restore_an_old_revision(self):
        self.import_tickets(self.tickets[:2])
        seen = self.detail()
        self.import_tickets(self.tickets[2:3])
        self.assertEqual(self.exclude(self.detail(), "booking-history").status_code, 200)
        current = self.detail()
        self.assertEqual(current["ticket_ids"], seen["ticket_ids"])
        self.assertEqual(current["examples"], seen["examples"])
        self.assertNotEqual(current["revision"], seen["revision"])
        self.assertEqual(self.submit_review(seen).status_code, 409)
        self.assertEqual(self.submit_review(seen, "rejected").status_code, 409)
        self.assertEqual(self.exclude(seen, "booking-1").status_code, 409)
        self.assertEqual(self.submit_review(current).status_code, 200)

    def test_date_backfill_changes_pending_revision_and_replay_does_not(self):
        self.import_tickets([{k: v for k, v in ticket.items() if k != "occurred_at"}
                             for ticket in self.tickets[:2]])
        seen = self.detail()
        self.assertIsNone(seen["sources"][0]["occurred_at"])
        self.assertEqual(self.import_tickets(self.tickets[:2])["staged_new"], 0)
        current = self.detail()
        self.assertEqual(current["sources"][0]["occurred_at"], "2026-11-01T06:00:00+00:00")
        self.assertNotEqual(current["revision"], seen["revision"])
        self.assertEqual(self.submit_review(seen).status_code, 409)
        equivalent = dict(self.tickets[0], occurred_at="2026-11-01T06:00:00Z")
        self.assertEqual(self.import_tickets([equivalent])["staged_new"], 0)
        self.assertEqual(self.detail()["revision"], current["revision"])

    def test_date_conflict_and_invalid_date_roll_back_batch(self):
        self.import_tickets(self.tickets[:2])
        seen = self.detail()
        for bad_date in ("2026-11-03T00:00:00Z", "2026-11-01T06:00:00",
                         "0001-01-01T00:00:00+01:00"):
            with self.subTest(date=bad_date):
                self.import_tickets([
                    self.tickets[2], dict(self.tickets[0], occurred_at=bad_date)
                ], status=409)
                self.assertEqual(self.detail(), seen)
        self.assertEqual(self.import_tickets(self.tickets[2:3])["staged_new"], 1)

    def test_exclusion_removes_excerpt_and_survives_reimport(self):
        self.import_tickets()
        response = self.exclude(self.detail(), "booking-history")
        self.assertEqual(response.status_code, 200, response.text)
        corrected = self.detail()
        self.assertEqual(corrected["ticket_ids"], ["booking-1", "booking-2"])
        self.assertNotIn("старый талон", json.dumps(corrected, ensure_ascii=False))
        self.assertEqual(self.import_tickets()["staged_new"], 0)
        self.assertEqual(self.detail(), corrected)
        timeline = self.client.get(self.url + "/timeline").json()
        excluded = [event for event in timeline if event["type"] == "source_excluded"]
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0]["ticket_id"], "booking-history")
        self.assertEqual(excluded[0]["actor"], "demo-reviewer")

    def test_product_with_slash_can_be_reviewed_and_handed_off(self):
        product = "Кабинет/Бронирование"
        self.import_tickets([dict(ticket, product=product) for ticket in self.tickets[:3]])
        signal_id = quote(product + ":bug:booking_queue", safe="")
        self.url = "/signals/" + signal_id
        self.assertEqual(self.exclude(self.detail(), "booking-history").status_code, 200)
        self.assertEqual(self.submit_review(self.detail()).status_code, 200)
        response = self.client.get(self.url + "/audit")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()[0]["action"], "approved")
        decision = self.client.post("/product-queue/" + signal_id + "/decision", json={
            "decision": "known", "reason": "Есть задача в бэклоге",
        })
        self.assertEqual(decision.status_code, 200, decision.text)
        self.assertEqual(self.queue()[0]["snapshot"]["ticket_count"], 2)
        history = self.client.get(self.url + "/timeline")
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json()[-1]["type"], "product_decision")

    def test_signal_routes_keep_trailing_slash_redirects(self):
        for method, path in (
            ("GET", "/signals/"),
            ("GET", self.url + "/"),
            ("GET", self.url + "/card/"),
            ("GET", self.url + "/timeline/"),
            ("POST", self.url + "/review/"),
        ):
            with self.subTest(method=method, path=path):
                response = self.client.request(method, path, follow_redirects=False)
                self.assertEqual(response.status_code, 307, response.text)

    def test_card_routes_keep_product_with_line_break_addressable(self):
        product = "Кабинет\nБронирование"
        self.import_tickets([dict(ticket, product=product) for ticket in self.tickets[:2]])
        signal_id = product + ":bug:booking_queue"
        self.url = "/signals/" + quote(signal_id, safe="")
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertEqual(self.detail()["id"], signal_id)
        self.assertEqual(self.submit_review(self.detail()).status_code, 200)
        self.assertEqual(self.client.get(self.url + "/timeline").status_code, 200)

    def test_card_suffix_disambiguates_semantic_ids_with_slashes(self):
        from pulse import Ticket
        from service import ExactTokenProvider, ingest_batch
        from store import SignalStore

        groups = (
            ("a", "z-1", "Очередь ошибка 404 при печати пропуска"),
            ("a/audit", "z-2", "Бронирование ошибка 404 в истории старых талонов"),
            ("b/", "z-3", "Очередь ошибка 404 на экране записи"),
        )
        tickets = [Ticket(ticket_id, "P", text)
                   for first, second, text in groups for ticket_id in (first, second)]
        with closing(SignalStore(self.db)) as store:
            ingest_batch(store, tickets, mode="semantic", provider=ExactTokenProvider())
        for first, second, _ in groups:
            with self.subTest(anchor=first):
                signal_id = "P:bug:booking_queue:" + first
                encoded = quote(signal_id, safe="")
                self.url = "/signals/" + encoded
                card = self.detail()
                self.assertEqual(card["id"], signal_id)
                self.assertEqual(card["ticket_ids"], [first, second])
                self.assertEqual(self.submit_review(card).status_code, 200)
                decision = self.client.post("/product-queue/" + encoded + "/decision", json={
                    "decision": "known", "reason": "Есть задача в бэклоге",
                })
                self.assertEqual(decision.status_code, 200, decision.text)
        self.assertEqual(len(self.queue()), 3)

    def test_exclusion_preserves_exact_cli_identifiers(self):
        from service import import_tickets

        tickets = [dict(ticket, id=ticket_id) for ticket, ticket_id in
                   zip(self.tickets[:3], (" leading", "x" * 129, "ordinary"))]
        file = self.db.with_name("tickets.json")
        file.write_text(json.dumps(tickets), encoding="utf-8")
        import_tickets(self.db, file)
        for ticket_id in (" leading", "x" * 129):
            response = self.exclude(self.detail(), ticket_id)
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.detail()["ticket_ids"], ["ordinary"])

    def test_legacy_ids_require_source_provenance_before_approval(self):
        from store import SignalStore

        with closing(SignalStore(self.db)) as store:
            store.ingest([{
                "id": "Кабинет:bug:booking_queue", "product": "Кабинет",
                "category": "bug", "workflow": "booking_queue",
                "ticket_count": 2, "ticket_ids": ["booking-1", "booking-2"],
                "examples": ["Ранее сохраненный пример"],
            }])
        seen = self.detail()
        self.assertEqual(self.submit_review(seen).status_code, 409)
        self.import_tickets([
            dict(ticket, text="Кнопка Далее не работает в договоре") for ticket in self.tickets[:2]
        ])
        current = self.detail()
        self.assertEqual(seen, current)
        self.assertTrue(all(source["excerpt"] is None for source in current["sources"]))
        self.assertEqual(self.submit_review(current).status_code, 409)
        self.assertEqual(self.queue(), [])

    def test_reanalysis_links_sources_to_existing_card(self):
        self.import_tickets(self.tickets[:2])
        # В базе до обновления были источники, но не было записи об их связи с карточкой.
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("DELETE FROM signal_sources")
            signal_id, raw = db.execute("SELECT id,payload FROM signals").fetchone()
            legacy = dict(json.loads(raw), examples=["Исторический пример"])
            db.execute("UPDATE signals SET payload=? WHERE id=?", (json.dumps(legacy), signal_id))
        seen = self.detail()
        self.assertTrue(all(source["excerpt"] is None for source in seen["sources"]))
        self.assertEqual(seen["examples"], ["Исторический пример"])
        self.assertEqual(self.import_tickets(self.tickets[:2])["staged_new"], 0)
        linked = self.detail()
        self.assertNotEqual(linked["revision"], seen["revision"])
        self.assertEqual(linked["examples"], [ticket["text"] for ticket in self.tickets[:2]])
        self.assertEqual(linked["sources"][0]["occurred_at"], "2026-11-01T06:00:00+00:00")
        self.assertEqual(self.submit_review(seen).status_code, 409)
        self.assertEqual(self.submit_review(linked).status_code, 200)

    def test_approval_requires_two_remaining_sources(self):
        self.import_tickets(self.tickets[:2])
        self.assertEqual(self.exclude(self.detail(), "booking-1").status_code, 200)
        self.assertEqual(self.submit_review(self.detail()).status_code, 409)
        self.assertEqual(self.queue(), [])
        self.assertEqual(self.submit_review(self.detail(), "rejected").status_code, 200)
        self.assertEqual(self.queue(), [])

    def test_handoff_snapshot_and_owner_decision(self):
        self.import_tickets(self.tickets[:2])
        self.assertEqual(self.submit_review(self.detail()).status_code, 200)
        row, = self.queue()
        frozen = row["snapshot"]
        self.assertEqual(frozen, self.detail())
        self.assertEqual(frozen["status"], "approved")
        self.assertEqual(row["first_ticket_at"], "2026-11-01T06:00:00+00:00")
        self.import_tickets(self.tickets[2:])
        current = self.detail()
        self.assertEqual(current["ticket_count"], 3)
        self.assertEqual(current["status"], "approved")
        self.assertEqual(current["reviewed_revision"], frozen["revision"])
        self.assertNotEqual(current["revision"], frozen["revision"])
        self.assertEqual(self.queue(), [row])

        body = {"decision": "known", "reason": "Есть задача, почта owner@example.invalid"}
        with self.client_class(self.client.app) as anonymous:
            self.assertEqual(anonymous.get(self.queue_url).status_code, 401)
            self.assertEqual(anonymous.post(self.decision_url, json=body).status_code, 401)
        self.assertEqual(self.client.post(self.decision_url, json=body).status_code, 200)
        self.assertEqual(self.client.post(self.decision_url, json=body).status_code, 409)
        saved, = self.queue()
        self.assertEqual(saved["decision"], "known")
        self.assertEqual(saved["decided_by"], "demo-reviewer")
        self.assertEqual(saved["reason"], "Есть задача, почта [EMAIL]")
        self.assertTrue(saved["decided_at"])
        self.assertEqual(saved["snapshot"], frozen)
        event = self.client.get(self.url + "/timeline").json()[-1]
        self.assertEqual(event["type"], "product_decision")
        self.assertEqual(event["actor"], saved["decided_by"])

    def test_decision_uses_snapshot_even_when_source_dates_are_backfilled(self):
        self.import_tickets([{k: v for k, v in ticket.items() if k != "occurred_at"}
                             for ticket in self.tickets[:2]])
        self.assertEqual(self.submit_review(self.detail()).status_code, 200)
        row, = self.queue()
        self.assertIsNone(row["first_ticket_at"])
        self.import_tickets(self.tickets[:2])
        self.assertEqual(self.detail()["sources"][0]["occurred_at"], "2026-11-01T06:00:00+00:00")
        self.assertIsNone(row["snapshot"]["sources"][0]["occurred_at"])
        self.assertEqual(self.queue(), [row])

    def test_concurrent_reviews_commit_only_one_decision(self):
        self.import_tickets()
        seen = self.detail()
        simultaneous = Barrier(2, timeout=5)
        read_before_lock = Barrier(2, timeout=5)
        connect = sqlite3.connect

        class CoordinatedConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                cursor = super().execute(sql, parameters)
                # Обе проверки видят pending, если чтение происходит до захвата записи.
                if sql.lstrip().startswith("SELECT") and "FROM signals" in sql and not self.in_transaction:
                    read_before_lock.wait()
                return cursor

        def review(action):
            simultaneous.wait()
            return self.submit_review(seen, action).status_code

        def coordinated_connect(path):
            return connect(path, factory=CoordinatedConnection)

        with patch("store.sqlite3.connect", new=coordinated_connect):
            with ThreadPoolExecutor(max_workers=2) as pool:
                codes = list(pool.map(review, ("approved", "rejected")))
        self.assertEqual(sorted(codes), [200, 409])
        audit = self.client.get(self.url + "/audit").json()
        self.assertEqual(len(audit), 1)
        self.assertEqual(len(self.queue()), int(audit[0]["action"] == "approved"))

    def test_queue_failure_rolls_back_approval_and_audit(self):
        self.import_tickets()
        seen = self.detail()
        with closing(sqlite3.connect(self.db)) as db:
            db.executescript("""
                CREATE TRIGGER fail_handoff BEFORE INSERT ON product_queue BEGIN
                    SELECT RAISE(ABORT, 'simulated queue failure');
                END;
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.submit_review(seen)
        self.assertEqual(self.detail(), seen)
        self.assertEqual(self.client.get(self.url + "/audit").json(), [])
        self.assertEqual(self.queue(), [])
