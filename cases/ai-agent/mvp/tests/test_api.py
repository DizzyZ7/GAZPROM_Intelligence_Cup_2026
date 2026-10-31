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
        self.assertEqual(len(self.client.get("/signals").json()),1)

    def test_invalid_status(self):
        self.assertEqual(self.client.get("/signals?status=invalid").status_code,422)

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

if __name__=="__main__":
    unittest.main()
