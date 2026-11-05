import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

class APITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import fastapi
            import httpx
        except ImportError:
            raise unittest.SkipTest("Для API-тестов установите requirements-api.txt")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"PULSE_DB":str(Path(self.tmp.name)/"demo.db"),
                                           "PULSE_REVIEW_TOKEN":"secret-test-token"})
        self.env.start()
        import api
        self.api = importlib.reload(api)
        from fastapi.testclient import TestClient
        self.client = TestClient(self.api.app)
        from store import SignalStore
        s=SignalStore(os.environ["PULSE_DB"])
        s.ingest([{"id":"P:bug:queue","product":"P","category":"bug",
                   "workflow":"queue","ticket_count":2,"ticket_ids":["1","2"],"examples":[]}])
        s.close()

    def tearDown(self):
        self.client.close()
        self.env.stop()
        self.tmp.cleanup()

    def test_health(self):
        self.assertEqual(self.client.get("/health").status_code,200)

    def test_context_mode_groups_across_imports_and_exposes_evidence(self):
        headers = {"X-Review-Token": "secret-test-token"}
        first = {"id": "a", "product": "Q", "text": "Скачивание отчета возвращает HTTP 404"}
        second = {"id": "b", "product": "Q", "text": "При попытке скачать отчет появилась ошибка 404"}
        for tickets, expected in (([first], 0), ([second], 1), ([second], 0)):
            response = self.client.post("/imports", headers=headers,
                                        json={"mode": "context", "tickets": tickets})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["new_signals"], expected)
            if tickets == [first]:
                self.assertEqual(response.json()["ungrouped"], [{"ticket_id": "a", "reason": "no_matching_cluster"}])
            else:
                self.assertEqual(response.json()["ungrouped"], [])
        signals = self.client.get("/signals", headers=headers).json()
        identity = next(row["id"] for row in signals if row["product"] == "Q")
        detail = self.client.get(f"/signals/{identity}", headers=headers).json()
        self.assertEqual(detail["ticket_ids"], ["a", "b"])
        self.assertEqual(detail["analysis_version"], "context-v1")
        self.assertEqual(detail["context"], {"operation": "report_download", "symptom": "http_404"})

    def test_context_migration_refuses_split_of_reviewed_signal_and_rolls_back(self):
        headers = {"X-Review-Token": "secret-test-token"}
        tickets = [{"id": identity, "product": "Q", "text": f"Очередь ошибка {code}"}
                   for identity, code in (("a", 404), ("b", 404), ("c", 500), ("d", 500))]
        self.assertEqual(self.client.post("/imports", headers=headers, json={"tickets": tickets}).status_code, 200)
        signal = "/signals/Q:bug:booking_queue"
        response = self.client.post(f"{signal}/review", headers=headers,
                                    json={"action": "approved", "reason": "Решение аналитика"})
        self.assertEqual(response.status_code, 200, response.text)
        additional = {"id": "new", "product": "Q", "text": "Очередь ошибка 500"}
        response = self.client.post("/imports", headers=headers,
                                    json={"mode": "context", "tickets": [additional]})
        self.assertEqual(response.status_code, 409, response.text)
        detail = self.client.get(signal, headers=headers).json()
        self.assertEqual(detail["status"], "approved")
        self.assertEqual(detail["ticket_ids"], ["a", "b", "c", "d"])
        self.assertEqual(len(self.client.get(f"{signal}/timeline", headers=headers).json()), 2)
        from store import SignalStore
        store = SignalStore(os.environ["PULSE_DB"])
        try:
            self.assertEqual({ticket.id for ticket in store.staged_tickets()}, {"a", "b", "c", "d"})
        finally:
            store.close()

    def test_context_migration_refuses_disappearance_below_minimum_cluster_size(self):
        headers = {"X-Review-Token": "secret-test-token"}
        tickets = [{"id": identity, "product": "Q", "text": f"Очередь ошибка {code}"}
                   for identity, code in (("a", 404), ("b", 500))]
        response = self.client.post("/imports", headers=headers, json={"tickets": tickets})
        self.assertEqual(response.status_code, 200, response.text)
        signal = "/signals/Q:bug:booking_queue"
        response = self.client.post(f"{signal}/review", headers=headers,
                                    json={"action": "approved", "reason": "Решение аналитика"})
        self.assertEqual(response.status_code, 200, response.text)
        new_ticket = {"id": "new", "product": "Q", "text": "Как скачать отчет?"}
        response = self.client.post("/imports", headers=headers,
                                    json={"mode": "context", "tickets": [new_ticket]})
        self.assertEqual(response.status_code, 409, response.text)
        detail = self.client.get(signal, headers=headers).json()
        self.assertEqual(detail["status"], "approved")
        self.assertEqual(detail["ticket_ids"], ["a", "b"])
        from store import SignalStore
        store = SignalStore(os.environ["PULSE_DB"])
        try:
            self.assertEqual({ticket.id for ticket in store.staged_tickets()}, {"a", "b"})
        finally:
            store.close()

    def test_list(self):
        self.assertEqual(len(self.client.get("/signals",headers={"X-Review-Token":"secret-test-token"}).json()),1)

    def test_invalid_status(self):
        self.assertEqual(self.client.get("/signals?status=invalid",headers={"X-Review-Token":"secret-test-token"}).status_code,422)

    def test_review_needs_token(self):
        self.assertEqual(self.client.post("/signals/P:bug:queue/review",json={"action":"approved","reason":"verified"}).status_code,401)

    def test_review_and_audit(self):
        h={"X-Review-Token":"secret-test-token"}
        r=self.client.post("/signals/P:bug:queue/review",headers=h,json={"action":"approved","reason":"verified"})
        self.assertEqual(r.status_code,200)
        self.assertEqual(self.client.get("/signals/P:bug:queue/audit",headers=h).json()[0]["action"],"approved")

    def test_no_repeat_review(self):
        h={"X-Review-Token":"secret-test-token"}
        url="/signals/P:bug:queue/review"
        self.client.post(url,headers=h,json={"action":"approved","reason":"verified"})
        self.assertEqual(self.client.post(url,headers=h,json={"action":"approved","reason":"verified"}).status_code,409)

    def test_list_requires_token(self):
        self.assertEqual(self.client.get("/signals").status_code,401)

    def test_import_requires_token(self):
        payload={"tickets":[{"id":"a","product":"P","text":"Очередь ошибка 404"},{"id":"b","product":"P","text":"Очередь ошибка 404"}]}
        self.assertEqual(self.client.post("/imports",json=payload).status_code,401)

    def test_import_and_timeline(self):
        headers={"X-Review-Token":"secret-test-token"}
        payload={"tickets":[{"id":"a","product":"Q","text":"Очередь ошибка 404"},{"id":"b","product":"Q","text":"Очередь ошибка 404"}]}
        response=self.client.post("/imports",headers=headers,json=payload)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["new_signals"],1)
        detail=self.client.get("/signals/Q:bug:booking_queue",headers=headers)
        self.assertEqual(detail.status_code,200,detail.text)
        self.assertEqual(detail.json()["ticket_count"],2)
        timeline=self.client.get("/signals/Q:bug:booking_queue/timeline",headers=headers)
        self.assertEqual(timeline.status_code,200,timeline.text)
        self.assertEqual(timeline.json()[0]["type"],"evidence_added")
        repeated=self.client.post("/imports",headers=headers,json=payload)
        self.assertEqual(repeated.json()["new_signals"],0)

    def test_import_duplicate_ids(self):
        headers={"X-Review-Token":"secret-test-token"}
        ticket={"id":"x","product":"Q","text":"Очередь ошибка 404"}
        self.assertEqual(self.client.post("/imports",headers=headers,json={"tickets":[ticket,ticket]}).status_code,422)

    def test_dashboard_assets(self):
        self.assertEqual(self.client.get("/dashboard").status_code,200)
        self.assertEqual(self.client.get("/assets/dashboard.js").status_code,200)
        self.assertEqual(self.client.get("/assets/unknown.js").status_code,404)

    def test_import_rejects_unknown_fields(self):
        headers={"X-Review-Token":"secret-test-token"}
        ticket={"id":"a","product":"P","text":"Очередь ошибка 404","secret":"unexpected"}
        response=self.client.post("/imports",headers=headers,json={"tickets":[ticket]})
        self.assertEqual(response.status_code,422,response.text)

    def test_import_rejects_non_string_fields(self):
        headers={"X-Review-Token":"secret-test-token"}
        ticket={"id":123,"product":"P","text":"Очередь ошибка 404"}
        self.assertEqual(self.client.post("/imports",headers=headers,json={"tickets":[ticket]}).status_code,422)

    def test_import_rejects_long_text(self):
        headers={"X-Review-Token":"secret-test-token"}
        ticket={"id":"a","product":"P","text":"x"*10001}
        self.assertEqual(self.client.post("/imports",headers=headers,json={"tickets":[ticket]}).status_code,422)

    def test_validation_does_not_echo_sensitive_input(self):
        headers={"X-Review-Token":"secret-test-token"}
        ticket={"id":"private","product":"P","text":"private@example.invalid " * 1000,
                "password":"do-not-reflect-this"}
        response=self.client.post("/imports",headers=headers,json={"tickets":[ticket]})
        self.assertEqual(response.status_code,422)
        self.assertNotIn("private@example.invalid", response.text)
        self.assertNotIn("do-not-reflect-this", response.text)
        self.assertNotIn('"input"', response.text)

    def test_signal_emerges_across_batches(self):
        headers={"X-Review-Token":"secret-test-token"}
        first={"tickets":[{"id":"first","product":"R","text":"Очередь ошибка 404"}]}
        second={"tickets":[{"id":"second","product":"R","text":"Очередь ошибка 404"}]}
        r1=self.client.post("/imports",headers=headers,json=first)
        self.assertEqual(r1.status_code,200,r1.text)
        self.assertEqual(r1.json()["signals_found"],0)
        r2=self.client.post("/imports",headers=headers,json=second)
        self.assertEqual(r2.status_code,200,r2.text)
        self.assertEqual(r2.json()["new_signals"],1)
        self.assertEqual(r2.json()["analyzed_total"],2)

    def test_ticket_id_conflict(self):
        headers={"X-Review-Token":"secret-test-token"}
        first={"tickets":[{"id":"unique","product":"R","text":"Очередь ошибка 404"}]}
        second={"tickets":[{"id":"unique","product":"S","text":"Очередь ошибка 404"}]}
        self.assertEqual(self.client.post("/imports",headers=headers,json=first).status_code,200)
        self.assertEqual(self.client.post("/imports",headers=headers,json=second).status_code,409)

    def test_staging_redacts_personal_data(self):
        headers={"X-Review-Token":"secret-test-token"}
        payload={"tickets":[{"id":"pii","product":"R",
            "text":"Очередь ошибка 404, почта alice@example.com, телефон +7 999 123 45 67"}]}
        response=self.client.post("/imports",headers=headers,json=payload)
        self.assertEqual(response.status_code,200,response.text)
        from store import SignalStore
        store=SignalStore(os.environ["PULSE_DB"])
        try:
            row=store.db.execute("SELECT text FROM staged_tickets WHERE id='pii'").fetchone()
            self.assertNotIn("alice@example.com",row["text"])
            self.assertNotIn("999 123 45 67",row["text"])
            self.assertIn("[EMAIL]",row["text"])
            self.assertIn("[PHONE]",row["text"])
        finally:
            store.close()

if __name__=="__main__":
    unittest.main()
