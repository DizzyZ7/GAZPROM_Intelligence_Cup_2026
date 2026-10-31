# Senior-review: reuse Intelligent Support Orchestrator

Источник: https://github.com/DizzyZ7/Intelligent-Support-Orchestrator-with-RAG-Async-Processing (README, docker-compose, app/services/*.py, app/worker/tasks.py; просмотрено 2026-10-31).

## Что реально подтверждено кодом
- FastAPI/Celery/Redis/Qdrant/LangChain и Docker Compose.
- `llm_service.py`: RAG-ответ из Qdrant с OpenAIEmbeddings и ChatOpenAI; это не агрегатор продуктовых сигналов.
- `ingestion.py`: индексация Markdown базы знаний; не ingestion обращений.
- `idempotency.py`: Redis lease с TTL и atomic Lua release/complete, ключ зависит от ticket_id + question.
- `zendesk.py`: приватная заметка Zendesk и reconciliation по marker, но конкретная система конкурса неизвестна.
- `worker/tasks.py`: retry для HTTP 429/5xx, обработка Retry-After; при busy возвращает in_progress без гарантированного повторного запуска.
- В репозитории имеются тесты безопасности ingestion, RAG, интеграций и worker; тесты в рамках этого ревью не запускались.

## Решение о переносе
| Элемент | Решение | Причина |
|---|---|---|
| FastAPI + Celery + Redis | Reuse patterns | Асинхронный batch-анализ не мешает поддержке |
| Redis lease | Adapt | Ключ должен учитывать ticket revision и тип задачи, busy требует requeue/lease recovery |
| Qdrant | Adapt | Индекс продуктовых сигналов отдельно от базы знаний; локальные embeddings |
| Zendesk adapter | Interface only | В кейсе нет подтверждения Zendesk |
| RAG answer generation | Do not reuse as core | Цель — агрегировать запросы, а не отвечать клиентам |
| OpenAIEmbeddings/ChatOpenAI | Replace | Запрет внешних LLM/API; внутренние Qwen, ASR, VL |
| Knowledge-base ingestion | Optional | Полезен для различения уже документированной функции и нового пожелания, но не основной источник |
| Idempotent delivery marker | Reuse concept | Уникальность отправки сигнала и журнал согласований |

## Исправления перед прототипом
1. Структурированный JSON с валидацией, evidence spans и abstain.
2. Мультимодальный pipeline: ASR для звонков, VL для изображений, лимиты и fallback.
3. Классификация: bug/feature_request/how_to/service_request/other; не все жалобы становятся пожеланиями.
4. Сопоставление по product_id + workflow + semantic similarity + review; не склеивать разные причины по одной ошибке.
5. Состояния сигнала: candidate → review → approved/rejected → delivered → feedback; RBAC.
6. Отдельный immutable audit trail и права на просмотр исходных тикетов.
7. Метрики ложных объединений, time-to-signal, acceptance rate, человеко-часы; baseline.
8. Security: prompt injection, ПДн, разграничение продуктов, запрет исходящих внешних запросов.
9. Протокол тестирования на синтетических и разрешенных обезличенных обращениях.
10. Конкурсная подача — самостоятельная реализация под требования кейса; старый проект лишь инженерная основа.

## Важные ограничения
Наличие кода не равно проверенной работоспособности: CI и нагрузочные тесты не запускались в этом ревью. Точный состав локальных моделей, API тикет-системы, объем обращений и целевая система product backlog требуют уточнения.
