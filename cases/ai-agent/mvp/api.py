"""Демонстрационный API проверки сигналов. Только для локального запуска."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from fastapi.responses import FileResponse
from fastapi import FastAPI, Header, HTTPException, Depends
from pydantic import BaseModel, Field
from store import SignalStore
from evidence import get_signal
from pulse import Ticket, analyze

app = FastAPI(title="ТИИм Pulse — проверка сигналов", version="0.5.0")
DB_PATH = os.getenv("PULSE_DB", str(Path(__file__).with_name("pulse-demo.sqlite3")))
TOKEN = os.getenv("PULSE_REVIEW_TOKEN")

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

@app.get("/health")
def health():
    return {"status": "ok", "mode": "local-demo"}

@app.get("/signals")
def signals(status: str | None = None):
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
    events = [
        {"type": "evidence_added", "at": row["created_at"],
         "added_ticket_ids": row["added_ticket_ids"]}
        for row in evidence
    ] + [
        {"type": "review", "at": row["created_at"],
         "actor": row["actor"], "action": row["action"], "reason": row["reason"]}
        for row in decisions
    ]
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

class ImportRequest(BaseModel):
    tickets: list[dict] = Field(min_length=1, max_length=500)

@app.post("/imports")
def import_batch(payload: ImportRequest, actor: str = Depends(reviewer)):
    if any(set(item) - {"id","product","text","channel"} for item in payload.tickets):
        raise HTTPException(status_code=422, detail="Неизвестные поля обращения")
    try:
        tickets = [Ticket(**item) for item in payload.tickets]
    except TypeError as exc:
        raise HTTPException(status_code=422, detail="Некорректная структура обращения") from exc
    if any(not isinstance(t.id,str) or not isinstance(t.product,str) or
           not isinstance(t.text,str) or not t.id.strip() or
           not t.product.strip() or not t.text.strip() or
           len(t.text)>10000 for t in tickets):
        raise HTTPException(status_code=422, detail="Некорректные поля обращения")
    ids = [t.id for t in tickets]
    if len(set(ids)) != len(ids):
        raise HTTPException(status_code=422, detail="Повторяющиеся идентификаторы обращений")
    result = analyze(tickets)
    with storage() as store:
        try:
            added = store.ingest(result["signals"])
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"processed":result["processed"],"signals_found":len(result["signals"]),
            "new_signals":added,"mode":"rules"}
