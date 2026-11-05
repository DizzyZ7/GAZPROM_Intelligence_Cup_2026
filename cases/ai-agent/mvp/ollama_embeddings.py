"""Optional local Ollama embedding adapter. Never sends data to remote hosts."""
import ipaddress
import json
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Перенаправления embedding-запросов запрещены")


# Изолированный транспорт не использует proxy из окружения и redirect.
urlopen = build_opener(ProxyHandler({}), _NoRedirect()).open


class OllamaEmbeddingProvider:
    def __init__(self, model="nomic-embed-text", base_url="http://127.0.0.1:11434", timeout=20):
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("Only local HTTP Ollama endpoints are allowed")
        try:
            address = ipaddress.ip_address(parsed.hostname or "")
            if not address.is_loopback:
                raise ValueError("Ollama endpoint must be loopback")
        except ValueError as exc:
            if parsed.hostname != "localhost":
                raise ValueError("Ollama endpoint must be loopback") from exc
        if not model or not isinstance(model, str) or len(model) > 128:
            raise ValueError("Invalid embedding model")
        if not 0 < timeout <= 120:
            raise ValueError("Invalid timeout")
        self.url = base_url.rstrip("/") + "/api/embed"
        self.model = model
        self.timeout = timeout

    def embed(self, texts):
        if not texts:
            return []
        if any(not isinstance(text, str) for text in texts):
            raise ValueError("Embedding input must be text")
        body = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        request = Request(self.url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=self.timeout) as response:
            payload = json.load(response)
        vectors = payload.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ValueError("Ollama returned incorrect embedding count")
        if any(not isinstance(vector, list) or not vector or
               any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in vector)
               for vector in vectors):
            raise ValueError("Ollama returned invalid embedding values")
        dims = {len(vector) for vector in vectors}
        if len(dims) != 1:
            raise ValueError("Ollama returned inconsistent embedding dimensions")
        from math import isfinite
        if any(not isfinite(value) for vector in vectors for value in vector):
            raise ValueError("Ollama returned nonfinite embedding values")
        return vectors
