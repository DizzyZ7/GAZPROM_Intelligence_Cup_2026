# ТИИм Pulse — MVP v0.2

Офлайн baseline для кейса Intelligence Cup. Синтетические примеры; без внешних LLM/API и без реальных персональных данных.

Запуск из этой папки:

```bash
python pulse.py
python cli.py --db demo.sqlite3 import
python cli.py --db demo.sqlite3 list
python cli.py --db demo.sqlite3 review 'Вебнефть:bug:booking_queue' --action approved --actor analyst --reason 'Проверено'
python cli.py --db demo.sqlite3 audit 'Вебнефть:bug:booking_queue'
python -m unittest discover -s tests -v
```

SQLite сохраняет ручные решения и audit trail. Повторный импорт не перезаписывает решения. Проверено локально: 10/10 тестов, 7 синтетических обращений, 2 сигнала, повторный импорт — 0 новых. Эвристический score не является калиброванной вероятностью. `--actor` не аутентифицируется. Нет RBAC, Qwen, интеграции с тикет-системой или автоматической отправки в бэклог.
