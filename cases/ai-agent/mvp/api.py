"""Демонстрационный API проверки сигналов. Только для локального запуска."""
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from fastapi.responses import FileResponse, JSONResponse
from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ConfigDict, StrictStr
from store import SignalStore
from evidence import get_signal
from pulse import Ticket
from service import ingest_batch

app = FastAPI(title="ТИИм Pulse — проверка сигналов", version="0.5.0")
DB_PATH = os.getenv("PULSE_DB", str(Path(__file__).with_name("pulse-demo.sqlite3")))
TOKEN = os.getenv("PULSE_REVIEW_TOKEN")

@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Вход и имена произвольных неизвестных полей могут содержать ПДн/секреты.
    return JSONResponse(status_code=422, content={"detail": "Запрос не соответствует схеме API"})

@contextmanager
def storage():
    store = SignalStore(DB_PATH)
    try:
        yield store
    finally:
        store.close()

def reviewer(x_review_token: str | None = Header(default=None)) -> str:
    if not TOKEN:
        raise HTTPException(status_code=503, detail="Токен рецензента не настроен")
    if not x_review_token or not __import__('hmac').compare_digest(x_review_token, TOKEN):
        raise HTTPException(status_code=401, detail="Нет доступа")
    return "demo-reviewer"

class ReviewRequest(BaseModel):
    action: str
    reason: str = Field(min_length=3, max_length=1000)

class ExclusionRequest(BaseModel):
    ticket_id: StrictStr = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=3, max_length=1000)

class ProductDecision(BaseModel):
    decision: Literal["in_progress", "known", "needs_clarification"]
    reason: str = Field(min_length=3, max_length=1000)

@app.post("/signals/{signal_id}/exclude")
def exclude(signal_id: str, payload: ExclusionRequest, actor: str = Depends(reviewer)):
    with storage() as store:
        try:
            store.exclude_source(signal_id, payload.ticket_id, actor, payload.reason)
            return get_signal(store, signal_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Сигнал не найден") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

@app.get("/product-queue")
def product_queue(actor: str = Depends(reviewer)):
    with storage() as store:
        return store.queue()

@app.post("/product-queue/{signal_id}/decision")
def product_decision(signal_id: str, payload: ProductDecision, actor: str = Depends(reviewer)):
    with storage() as store:
        try:
            store.decide(signal_id, payload.decision, payload.reason)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Сигнал не передан владельцу") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"signal_id": signal_id, "decision": payload.decision}

@app.get("/health")
def health():
    return {"status": "ok", "mode": "local-demo"}

@app.get("/signals")
def signals(status: str | None = None, actor: str = Depends(reviewer)):
    with storage() as store:
        try:
            return store.list_signals(status)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

@app.get("/signals/{signal_id}/audit")
def audit(signal_id: str, actor: str = Depends(reviewer)):
    with storage() as store:
        if not any(x["id"] == signal_id for x in store.list_signals()):
            raise HTTPException(status_code=404, detail="Сигнал не найден")
        return store.audit(signal_id)

@app.post("/signals/{signal_id}/review")
def review(signal_id: str, request: ReviewRequest, actor: str = Depends(reviewer)):
    with storage() as store:
        try:
            store.review(signal_id, request.action, actor, request.reason)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Сигнал не найден") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"signal_id": signal_id, "status": request.action, "reviewer": actor}

@app.get("/signals/{signal_id}")
def signal_detail(signal_id: str, actor: str = Depends(reviewer)):
    with storage() as store:
        try:
            return get_signal(store, signal_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Сигнал не найден") from exc

@app.get("/signals/{signal_id}/timeline")
def signal_timeline(signal_id: str, actor: str = Depends(reviewer)):
    with storage() as store:
        try:
            evidence = store.evidence_history(signal_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Сигнал не найден") from exc
        decisions = store.audit(signal_id)
        exclusions = store.exclusions(signal_id)
    events = [
        {"type": "evidence_added", "at": row["created_at"],
         "added_ticket_ids": row["added_ticket_ids"]}
        for row in evidence
    ] + [
        {"type": "review", "at": row["created_at"],
         "actor": row["actor"], "action": row["action"], "reason": row["reason"]}
        for row in decisions
    ]
    events.extend(
        {"type": "source_excluded", "at": row["created_at"],
         "action": "Исключён источник " + row["ticket_id"],
         "actor": row["actor"], "reason": row["reason"]}
        for row in exclusions
    )
    return sorted(events, key=lambda event: event["at"])

@app.get("/dashboard", include_in_schema=False)
def dashboard():
    return FileResponse(Path(__file__).with_name("dashboard.html"), media_type="text/html", headers={"Cache-Control":"no-store","Content-Security-Policy":"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"})

@app.get("/assets/{asset_name}", include_in_schema=False)
def dashboard_asset(asset_name: str):
    allowed = {"dashboard.css": "text/css", "dashboard.js": "application/javascript"}
    if asset_name not in allowed:
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(Path(__file__).with_name(asset_name), media_type=allowed[asset_name])

class TicketInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: StrictStr = Field(min_length=1, max_length=128)
    product: StrictStr = Field(min_length=1, max_length=128)
    text: StrictStr = Field(min_length=1, max_length=10000)
    channel: StrictStr = Field(default="web", min_length=1, max_length=64)
    occurred_at: StrictStr | None = Field(default=None, max_length=40)

class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tickets: list[TicketInput] = Field(min_length=1, max_length=500)
    mode: Literal["rules", "experimental"] = "rules"

@app.post("/imports")
def import_batch(payload: ImportRequest, actor: str = Depends(reviewer)):
    tickets = [Ticket(**item.model_dump()) for item in payload.tickets]
    ids = [t.id for t in tickets]
    if len(set(ids)) != len(ids):
        raise HTTPException(status_code=422, detail="Повторяющиеся идентификаторы обращений")
    with storage() as store:
        try:
            result = ingest_batch(store, tickets, mode=payload.mode)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result
