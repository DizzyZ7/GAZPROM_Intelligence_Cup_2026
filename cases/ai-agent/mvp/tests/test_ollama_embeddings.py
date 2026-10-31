import json
import unittest
from unittest.mock import patch
from ollama_embeddings import OllamaEmbeddingProvider


class Response:
    def __init__(self, payload):
        self.payload = payload
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, *args):
        return json.dumps(self.payload).encode("utf-8")


class OllamaTests(unittest.TestCase):
    def test_only_loopback_allowed(self):
        for url in ("https://127.0.0.1:11434", "http://example.com:11434",
                    "http://192.168.1.5:11434", "http://127.0.0.1:11434/api/embed"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                OllamaEmbeddingProvider(base_url=url)

    def test_empty_skips_network(self):
        with patch("ollama_embeddings.urlopen") as call:
            self.assertEqual(OllamaEmbeddingProvider().embed([]), [])
            call.assert_not_called()

    def test_embed_batch(self):
        with patch("ollama_embeddings.urlopen", return_value=Response({"embeddings":[[1.0,0.0],[0.0,1.0]]})) as call:
            vectors=OllamaEmbeddingProvider().embed(["first","second"])
            self.assertEqual(vectors,[[1.0,0.0],[0.0,1.0]])
            request=call.call_args.args[0]
            self.assertEqual(request.full_url,"http://127.0.0.1:11434/api/embed")
            self.assertEqual(json.loads(request.data)["input"],["first","second"])

    def test_invalid_vectors(self):
        for payload in ({"embeddings":[]}, {"embeddings":[[1.0],[1.0,2.0]]},
                        {"embeddings":[["bad"],[1.0]]},
                        {"embeddings":[[float("nan")],[1.0]]}):
            with self.subTest(payload=payload), patch("ollama_embeddings.urlopen",return_value=Response(payload)):
                with self.assertRaises(ValueError):
                    OllamaEmbeddingProvider().embed(["first","second"])


if __name__ == "__main__":
    unittest.main()
