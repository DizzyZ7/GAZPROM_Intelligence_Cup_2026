"""Ограничение HTTP-тела до разбора JSON и вызова обработчиков."""

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_REQUEST_BODY_BYTES = 32 * 1024 * 1024


class RequestBodyLimit:
    def __init__(self, app: ASGIApp, max_bytes: int = MAX_REQUEST_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        lengths = [
            value.strip(b" \t")
            for name, value in scope["headers"]
            if name.lower() == b"content-length"
        ]
        if lengths:
            if len(lengths) != 1 or not lengths[0].isdigit():
                await JSONResponse(
                    {"detail": "Некорректный Content-Length"}, status_code=400
                )(scope, receive, send)
                return
            # Не преобразуем недоверенную строку произвольной длины в int.
            declared = lengths[0].lstrip(b"0") or b"0"
            limit = str(self.max_bytes).encode("ascii")
            if len(declared) > len(limit) or (
                len(declared) == len(limit) and declared > limit
            ):
                await self._too_large(scope, receive, send)
                return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(chunk) > self.max_bytes - len(body):
                await self._too_large(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        replayed = False

        async def buffered_receive() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        # Обработчик запускается только после проверки всего тела.
        await self.app(scope, buffered_receive, send)

    async def _too_large(self, scope: Scope, receive: Receive, send: Send) -> None:
        await JSONResponse(
            {"detail": "Тело запроса превышает допустимый размер"}, status_code=413
        )(scope, receive, send)
