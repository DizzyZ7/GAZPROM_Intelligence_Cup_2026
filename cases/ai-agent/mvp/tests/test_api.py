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

if __name__=="__main__":
    unittest.main()
