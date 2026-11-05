import json
import os
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError

from ollama_embeddings import OllamaEmbeddingProvider


@contextmanager
def endpoint(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class LocalHandler(BaseHTTPRequestHandler):
    received = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.received.append(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        body = json.dumps({"embeddings": [[1.0, 0.0]]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST


class TransportTests(unittest.TestCase):
    def test_redirect_never_reaches_second_endpoint(self):
        class Target(LocalHandler):
            received = []

        with endpoint(Target) as target:
            class Redirect(LocalHandler):
                def do_POST(self):
                    self.rfile.read(int(self.headers["Content-Length"]))
                    self.send_response(302)
                    self.send_header("Location", target + "/collect")
                    self.end_headers()

            with endpoint(Redirect) as origin:
                with self.assertRaises((ValueError, HTTPError)):
                    OllamaEmbeddingProvider(base_url=origin).embed(["Закрытые данные"])
        self.assertEqual(Target.received, [])

    def test_environment_proxy_cannot_receive_embedding_input(self):
        class Proxy(LocalHandler):
            received = []

        class Origin(LocalHandler):
            received = []

        with endpoint(Proxy) as proxy, endpoint(Origin) as origin:
            with patch.dict(os.environ, {"http_proxy":proxy, "HTTP_PROXY":proxy,
                                         "no_proxy":"", "NO_PROXY":""}):
                # Пересоздаем стандартный opener, чтобы проверить влияние окружения.
                import urllib.request
                with patch.object(urllib.request, "_opener", None):
                    vectors = OllamaEmbeddingProvider(base_url=origin).embed(["Закрытые данные"])
        self.assertEqual(vectors, [[1.0, 0.0]])
        self.assertEqual(Proxy.received, [])
        self.assertEqual(len(Origin.received), 1)


if __name__ == "__main__":
    unittest.main()
