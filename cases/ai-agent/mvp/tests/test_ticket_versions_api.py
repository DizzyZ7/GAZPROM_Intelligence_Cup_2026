"""Версии в публичном контракте импорта и истории карточки."""

import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from test_ticket_versions import ACTIVE, RESOLVED

from service import import_tickets
from store import SignalStore


class TicketVersionsAPITests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "api.db"
        env = patch.dict(
            os.environ,
            {"PULSE_DB": str(self.path), "PULSE_REVIEW_TOKEN": "version-test-token"},
        )
        env.start()
        self.addCleanup(env.stop)
        import api

        self.client = TestClient(importlib.reload(api).app)
        self.addCleanup(self.client.close)
        self.headers = {"X-Review-Token": "version-test-token"}
        self.url = "/signals/P:bug:booking_queue"

    def post(self, rows):
        return self.client.post(
            "/imports", headers=self.headers, json={"tickets": rows}
        )

    def seed(self):
        response = self.post(
            [{"id": i, "product": "P", "text": ACTIVE} for i in ("a", "b", "c")]
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_update_and_timeline_history_auth_and_replay(self):
        self.seed()
        row = {"id": "a", "product": "P", "text": RESOLVED, "version": 2}
        response = self.post([row])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["updated_tickets"], 1)
        history = self.client.get("/tickets/a/versions", headers=self.headers)
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual([r["version"] for r in history.json()], [1, 2])
        self.assertEqual(self.client.get("/tickets/a/versions").status_code, 401)
        self.assertEqual(
            self.client.get(
                "/tickets/absent/versions", headers=self.headers
            ).status_code,
            404,
        )
        events = self.client.get(self.url + "/timeline", headers=self.headers).json()
        update = [e for e in events if e["type"] == "ticket_updated"]
        self.assertEqual(len(update), 1)
        self.assertEqual(
            (update[0]["sources_before"], update[0]["sources_after"]), (3, 2)
        )
        self.assertEqual(self.post([row]).json()["updated_tickets"], 0)
        self.assertEqual(
            self.client.get(self.url + "/timeline", headers=self.headers).json(), events
        )
        self.assertEqual(self.post([dict(row, version=1)]).status_code, 409)
        self.assertEqual(self.post([dict(row, text=ACTIVE)]).status_code, 409)

    def test_version_schema_rejects_coercion(self):
        for version in (True, "2", 2.0, None, 0, -1, 2**63):
            with self.subTest(version=version):
                r = self.post(
                    [{"id": "a", "product": "P", "text": ACTIVE, "version": version}]
                )
                self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(
            self.post(
                [{"id": "a", "product": "P", "text": ACTIVE, "version": 1}]
            ).status_code,
            200,
        )

    def test_conflicting_batch_rolls_back_already_staged_update_and_new_ticket(self):
        self.seed()
        before = self.client.get(self.url, headers=self.headers).json()
        r = self.post(
            [
                {"id": "a", "product": "P", "text": RESOLVED, "version": 2},
                {"id": "new", "product": "P", "text": ACTIVE},
                {"id": "b", "product": "P", "text": "Другой текст", "version": 1},
            ]
        )
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self.client.get(self.url, headers=self.headers).json(), before)
        self.assertEqual(
            self.client.get("/tickets/new/versions", headers=self.headers).status_code,
            404,
        )
        self.assertEqual(
            len(self.client.get("/tickets/a/versions", headers=self.headers).json()), 1
        )

    def test_cli_import_supports_version_and_redacted_history(self):
        import json

        file = self.path.with_suffix(".json")
        file.write_text(
            json.dumps(
                [{"id": "a", "product": "P", "text": "alice@example.com", "version": 4}]
            )
        )
        import_tickets(self.path, file)
        file.write_text(
            json.dumps(
                [
                    {
                        "id": "a",
                        "product": "P",
                        "text": "Другие сведения bob@example.com",
                        "version": 5,
                    }
                ]
            )
        )
        self.assertEqual(import_tickets(self.path, file)["updated_tickets"], 1)
        store = SignalStore(self.path)
        self.addCleanup(store.close)
        with store.db as db:
            text = " ".join(
                r[0] for r in db.execute("SELECT text FROM ticket_versions")
            )
            self.assertNotIn("alice@", text)
            self.assertNotIn("bob@", text)
