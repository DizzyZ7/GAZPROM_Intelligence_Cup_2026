# Ticket Versions Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans inline, with one independent whole-branch review.

**Goal:** Обновлять обращения без дублей и объяснять изменение затронутых сигналов.
**Architecture:** Неизменяемая история + текущий источник + отдельный журнал сигналов.
**Tech Stack:** Python 3.11/3.12, SQLite, FastAPI, plain JS.
**Spec:** docs/superpowers/specs/2026-11-12-ticket-versions-design.md

## Global Constraints
- Совместимость старых импортов; номер версии строго int 1..2^63-1.
- Старая/конфликтующая версия: атомарный отказ, без перезаписи.
- История только обезличенная; неизменяемые подтвержденные снимки и ручные решения.
- Минимум два источника для подтверждения, без автоматического слияния разных тем.

## Review Focus
- Миграция БД и повторное открытие не создают версий или событий повторно.
- Два одновременных обновления не теряют новую версию.
- Пакет с одним конфликтом откатывает версии, состав сигналов и журнал целиком.
- Устранение/возврат/смена симптома не затрагивают соседний сигнал и ручное исключение.
- Длинный или недоверенный текст не попадает в журнал/ответы без обезличивания.

### Task 1: Неизменяемая история и контракт версий
Files: ticket_versions.py, store.py, service.py, api.py, tests/test_ticket_versions.py.
Interfaces: stage_versions(store,tickets,versions,ticket_dates) -> {inserted,updates};
SignalStore.stage_tickets прежний int; service.ingest_batch(...,ticket_versions=None).
- [x] Добавить failing-тесты: первая версия, повтор, конфликт, старая, миграция,
  обезличивание, откат и конкурентные обновления. Запустить и убедиться в RED.
- [x] Реализовать таблицы, миграцию, строгую валидацию и атомарное сохранение.
- [x] Прогнать новые тесты и старые тесты staging/API.

### Task 2: Пересчет затронутых сигналов
Files: signal_updates.py, service.py, evidence.py, store.py, tests/test_ticket_versions.py.
Interfaces: capture_signals, detach_incompatible_sources, record_signal_changes;
get_signal возвращает version у связанного источника и последнюю разницу.
- [x] Failing-тесты: устранение 3->2; новый симптом; возврат в старый сигнал;
  неизменный сосед и подтвержденный снимок; ручное исключение; полный откат.
- [x] Отделять только явно обновленные несовместимые источники; сохранить ID,
  историю, неподтвержденные singleton/empty карточки; проверять старые контексты.
- [x] Фиксировать одно событие на затронутый сигнал за пакет; версия входит в revision.
- [x] Прогнать полную регрессию.

### Task 3: API, панель и выпуск
Files: api.py, dashboard.js, README.md, .github/workflows/pulse-ci.yml, tests/test_api.py.
- [x] Failing API-тесты: version strict type, старая версия 409, история с токеном,
  timeline показывает old/new counts; конфликт в конце пакета откатывает все.
- [x] Добавить контракт и отображение версий, причин и различий с reviewed snapshot.
- [x] Проверить DOM, синтаксис JS, все тесты, Ruff/Bandit, аудит и сохраненные отчеты.
- [x] Независимое ревью; устранить блокирующие замечания с регрессиями.
- [ ] Опубликовать PR; дождаться CI и слить в main, проверить CI после слияния.
