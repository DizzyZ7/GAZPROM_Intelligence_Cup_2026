import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


async def http_exchange(app, messages, *, headers=(), path="/imports"):
    """Передать ASGI-чанки раздельно, без склейки транспортом TestClient."""
    incoming = iter(messages)
    responses = []
    reads = 0

    async def receive():
        nonlocal reads
        reads += 1
        return next(incoming, {"type": "http.disconnect"})

    async def send(message):
        responses.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": list(headers),
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        },
        receive,
        send,
    )
    status = next(
        (
            message["status"]
            for message in responses
            if message["type"] == "http.response.start"
        ),
        None,
    )
    body = b"".join(
        message.get("body", b"")
        for message in responses
        if message["type"] == "http.response.body"
    )
    return status, body, reads


class BodyLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from starlette.responses import Response

            from http_limits import RequestBodyLimit
        except ImportError as exc:
            raise unittest.SkipTest("Установите requirements-api.txt") from exc

        async def echo(scope, receive, send):
            message = await receive()
            await Response(message.get("body", b""))(scope, receive, send)

        cls.app = RequestBodyLimit(echo, max_bytes=8)

    def exchange(self, messages, headers=()):
        return asyncio.run(http_exchange(self.app, messages, headers=headers))

    def test_boundary_counts_utf8_bytes_and_preserves_body(self):
        for text, expected in (("", 200), ("яяяя", 200), ("яяяяx", 413)):
            with self.subTest(text=text):
                payload = text.encode()
                status, body, _ = self.exchange(
                    [
                        {
                            "type": "http.request",
                            "body": payload[:3],
                            "more_body": True,
                        },
                        {"type": "http.request", "body": payload[3:]},
                    ]
                )
                self.assertEqual(status, expected)
                if expected == 200:
                    self.assertEqual(body, payload)

    def test_missing_or_understated_length_cannot_bypass_limit(self):
        for headers in ((), ((b"content-length", b"0"),), ((b"Content-Length", b"1"),)):
            with self.subTest(headers=headers):
                status, body, reads = self.exchange(
                    [
                        {
                            "type": "http.request",
                            "body": b"12345678",
                            "more_body": True,
                        },
                        {"type": "http.request", "body": b"x", "more_body": True},
                        {"type": "http.request", "body": b"private@example.invalid"},
                    ],
                    headers,
                )
                self.assertEqual(status, 413)
                self.assertEqual(
                    reads, 2, "После превышения остаток потока не читается"
                )
                self.assertNotIn(b"12345678", body)
                self.assertNotIn(b"private@example.invalid", body)

    def test_declared_overflow_is_rejected_without_reading(self):
        status, _, reads = self.exchange([], ((b"content-length", b"9"),))
        self.assertEqual(status, 413)
        self.assertEqual(reads, 0)

    def test_invalid_or_duplicate_content_length_is_sanitized(self):
        for lengths in (
            (b"",),
            (b"-1",),
            (b"+1",),
            (b"1, 2",),
            (b"\x0b1",),
            (b"1\x0c",),
            (b"\r1\n",),
            (b"private@example.invalid",),
            (b"1", b"1"),
        ):
            with self.subTest(lengths=lengths):
                status, body, reads = self.exchange(
                    [],
                    [(b"content-length", value) for value in lengths],
                )
                self.assertEqual(status, 400)
                self.assertEqual(reads, 0)
                self.assertNotIn(b"private@example.invalid", body)

    def test_long_numeric_length_does_not_overflow_integer_parser(self):
        status, _, reads = self.exchange([], ((b"content-length", b"9" * 10000),))
        self.assertEqual(status, 413)
        self.assertEqual(reads, 0)
        status, body, _ = self.exchange(
            [{"type": "http.request", "body": b"x"}],
            ((b"content-length", b"0" * 10000 + b"1"),),
        )
        self.assertEqual((status, body), (200, b"x"))


class APIBodyLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from fastapi.testclient import TestClient
        except (ImportError, RuntimeError) as exc:
            raise unittest.SkipTest(
                "Для API-тестов установите requirements-api.txt"
            ) from exc
        cls.client_class = TestClient

    def setUp(self):
        import api

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "pulse.db"
        config = patch.multiple(
            api, DB_PATH=str(self.db), TOKEN="body-limit-test-token"
        )
        config.start()
        self.addCleanup(config.stop)
        self.client = self.client_class(
            api.app, headers={"X-Review-Token": "body-limit-test-token"}
        )
        self.addCleanup(self.client.close)
        self.app = api.app

    def test_oversized_declared_body_is_rejected_before_import(self):
        response = self.client.post(
            "/imports",
            headers={"Content-Length": "33554433"},
            json={
                "tickets": [
                    {"id": "first", "product": "P", "text": "Очередь ошибка 404"}
                ]
            },
        )
        self.assertEqual(response.status_code, 413, response.text)
        self.assertFalse(self.db.exists(), "Отказ не должен открывать хранилище")

    def test_all_mutation_routes_enforce_limit_before_storage(self):
        for path in (
            "/signals/P:bug:queue/review",
            "/signals/P:bug:queue/exclude",
            "/product-queue/P:bug:queue/decision",
        ):
            with self.subTest(path=path):
                response = self.client.post(
                    path,
                    headers={"Content-Length": "33554433"},
                    json={"reason": "Проверено"},
                )
                self.assertEqual(response.status_code, 413)
                self.assertFalse(self.db.exists())

    def test_chunked_overflow_does_not_import_even_a_complete_json_prefix(self):
        payload = json.dumps(
            {
                "tickets": [
                    {"id": "prefix", "product": "P", "text": "Очередь ошибка 404"},
                ]
            }
        ).encode()

        def chunks():
            yield {"type": "http.request", "body": payload, "more_body": True}
            for _ in range(32):
                yield {
                    "type": "http.request",
                    "body": b" " * 1048576,
                    "more_body": True,
                }
            yield {"type": "http.request", "body": b"private@example.invalid"}

        for headers in ((), ((b"content-length", b"1"),)):
            with self.subTest(headers=headers):
                status, body, reads = asyncio.run(
                    http_exchange(
                        self.app,
                        chunks(),
                        headers=[
                            (b"content-type", b"application/json"),
                            (b"x-review-token", b"body-limit-test-token"),
                            *headers,
                        ],
                    )
                )
                self.assertEqual(status, 413)
                self.assertEqual(reads, 33)
                self.assertNotIn(b"private@example.invalid", body)
                self.assertFalse(self.db.exists())

    def test_disconnect_before_end_of_body_never_opens_storage(self):
        payload = json.dumps(
            {
                "tickets": [
                    {"id": "partial", "product": "P", "text": "Очередь ошибка 404"},
                ]
            }
        ).encode()
        status, body, reads = asyncio.run(
            http_exchange(
                self.app,
                [
                    {"type": "http.request", "body": payload, "more_body": True},
                    {"type": "http.disconnect"},
                ],
                headers=[
                    (b"content-type", b"application/json"),
                    (b"x-review-token", b"body-limit-test-token"),
                ],
            )
        )
        self.assertEqual((status, body, reads), (None, b"", 2))
        self.assertFalse(self.db.exists())

    def test_maximum_cyrillic_batch_with_json_escaping_is_accepted(self):
        text = "я" * 10000
        payload = json.dumps(
            {
                "tickets": [
                    {"id": str(index), "product": "Кабинет", "text": text}
                    for index in range(500)
                ]
            }
        ).encode()
        response = self.client.post(
            "/imports",
            headers={"Content-Type": "application/json"},
            content=payload,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["staged_new"], 500)

    def test_lifespan_and_public_routes_still_work(self):
        with self.client as client:
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/dashboard").status_code, 200)
        self.assertFalse(self.db.exists())
