"""Демонстрационный API проверки сигналов. Только для локального запуска."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from fastapi import FastAPI, Header, HTTPException, Depends
from pydantic import BaseModel, Field
from store import SignalStore
from evidence import get_signal

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
