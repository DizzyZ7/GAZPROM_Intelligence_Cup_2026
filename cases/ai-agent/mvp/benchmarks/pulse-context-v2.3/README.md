# Контекстный анализ и локальные эмбеддинги Pulse

Дата: 09.11.2026. Алгоритм: `context-v2.3`. Контроль: `pulse-independent-20261109` — development 300, validation 100, новый holdout 120. Параметры и исходные SHA зафиксированы до открытия нового holdout. После получения результата алгоритм и пороги не изменялись.

Исходные development/validation сохранены побайтно. Новый контроль составлен отдельно без чтения реализации, тестов и предсказаний. Это инженерная синтетика с ожидающей человеческой предметной проверкой; независимость подготовки не делает ее корпоративной выборкой.

Режим `rules` здесь использует общий контекстный экстрактор и нужен для отделения эффекта сходства от эффекта классификации. `legacy_rules` — прежние пять правил рабочего прототипа. TF-IDF и Embeddings используют те же предсказанные контекстные ограничения, что и `rules`. Алгоритмы поиска и кластеризации не получают эталонные метки. Все четыре метода формируют предсказания до чтения эталонов.

## Фактические результаты кластеризации

### development

| Метод | TP | FP | FN | Pairwise F1 |
| --- | ---: | ---: | ---: | ---: |
| legacy_rules | 130 | 357 | 407 | 0.25391 |
| rules | 152 | 0 | 385 | 0.44122 |
| tfidf | 140 | 0 | 397 | 0.41359 |
| embeddings | 139 | 0 | 398 | 0.41124 |

Обращений, допущенных к embedding-provider: 118.

### validation

| Метод | TP | FP | FN | Pairwise F1 |
| --- | ---: | ---: | ---: | ---: |
| legacy_rules | 0 | 1 | 71 | 0.00000 |
| rules | 7 | 0 | 64 | 0.17949 |
| tfidf | 6 | 0 | 65 | 0.15584 |
| embeddings | 7 | 0 | 64 | 0.17949 |

Обращений, допущенных к embedding-provider: 12.

### holdout

| Метод | TP | FP | FN | Pairwise F1 |
| --- | ---: | ---: | ---: | ---: |
| legacy_rules | 0 | 1 | 120 | 0.00000 |
| rules | 0 | 0 | 120 | 0.00000 |
| tfidf | 0 | 0 | 120 | 0.00000 |
| embeddings | 0 | 0 | 120 | 0.00000 |

Обращений, допущенных к embedding-provider: 0.

## Интерпретация

На знакомом development контекстные правила нашли 152 эталонные пары без ложных объединений. В прежнем эксперименте v2.2 на тех же development-признаках было 31 TP и 12 FP. Это улучшение на наборе разработки; оно не подтверждает переносимость.

На validation у контекстных правил и Embeddings 7 TP, 0 FP, 64 FN из 71 эталонной пары. Критерий роста TP выше 7 не выполнен. TF-IDF выбрал порог 0,05, BGE-M3 — 0,60; выбор сделан только на validation. Порог с высоким F1 в таком малом наборе не является промышленной рекомендацией.

На новом holdout все 120 обращений получили отказ от группировки до стадии сходства. Все 120 эталонных пар пропущены. Macro F1 категории у контекстных методов — 0,18475, у прежнего baseline — 0,02604. Это отдельная задача классификации, ее метрика не складывается с pairwise F1.

Ограничения контекста не пропустили ни одного кандидата holdout. Поэтому provider на этом split не вызывался: строка Embeddings описывает итоговый пайплайн, а не фактически выполненный инференс на holdout. Реальный BGE-M3 инференс выполнен для 118 кандидатов development и 12 validation, а также двух предварительных технических примеров. По этим результатам нельзя оценивать семантический потенциал модели на новых проблемах.

Нулевые FP при отсутствии любых объединений не доказывают хорошую точность. Не выполнен gate переноса контекстного режима в рабочий API; текущий анализ импорта не изменен. Отдельный файл `quality-gate.json` фиксирует отказ от продвижения.

## Воспроизведение и целостность

Все команды выполняются из `cases/ai-agent/mvp` на Python 3.11/3.12. Требуются установленные зависимости API и локальный Ollama с `bge-m3:567m`. Во время опыта использовались Ollama 0.40.15 и digest модели `7907646426070047a77226ac3e684fbbe8410524f7b4a74d02837e43f2146bab`; runtime и границы измерений находятся в `runtime.json`. Облако отключено, данные передавались только по loopback.

Проверка сохраненных результатов не запускает модель:

```bash
python -m benchmark verify --directory benchmarks/pulse-context-v2.3/development
python -m benchmark verify --directory benchmarks/pulse-context-v2.3/validation
python -m benchmark verify --directory benchmarks/pulse-context-v2.3/holdout
python datasets/pulse-independent-20261109/generate_dataset.py --check datasets/pulse-independent-20261109/benchmark
```

Для воспроизведения девяти файлов контроля сохраните неизменяемый рецепт вместе со снимками исходных данных:

```bash
mkdir -p /tmp/pulse-independent-repro
cp datasets/pulse-independent-20261109/generate_dataset.py /tmp/pulse-independent-repro/
cp -R datasets/pulse-independent-20261109/source_snapshots /tmp/pulse-independent-repro/
python /tmp/pulse-independent-repro/generate_dataset.py --verify-reproducible
python /tmp/pulse-independent-repro/generate_dataset.py --check /tmp/pulse-independent-repro/benchmark
```

Существующие данные не перезаписываются. Повторная оценка опубликованного holdout для настройки запрещена; для следующего алгоритма потребуется другой контроль. Пример процедуры для будущего, еще не открытого набора:

```bash
python -m benchmark tune --dataset /path/to/new-dataset --output /tmp/pulse-new-calibration --classifier context --ollama-model bge-m3:567m
python -m benchmark evaluate --dataset /path/to/new-dataset --calibration /tmp/pulse-new-calibration --output /tmp/pulse-new-validation --split validation --ollama-model bge-m3:567m
python -m benchmark evaluate --dataset /path/to/new-dataset --calibration /tmp/pulse-new-calibration --output /tmp/pulse-new-holdout --ollama-model bge-m3:567m
```

Квитанции каждой выборки одноразовые; frozen config связывает код, модель, dataset manifest, TF-IDF и выбранные пороги. CSV предсказаний, отдельные JSON-метрики классификации/поиска/кластеризации, матрицы ошибок и примеры промахов находятся в каталогах методов каждого split.

## Следующий приоритет

Узкое место — извлечение категории и сценария на незнакомых темах. Следующий эксперимент должен проверить локальное структурированное извлечение контекста со строгой серверной схемой и сохранением отказа при неопределенности. Требуются независимая человеческая проверка части разметки и новый контроль после изменения алгоритма. Расширение словарных правил по опубликованному holdout не будет независимой оценкой.

Для проверки разметки подготовлен [слепой пакет](../../reviews/pulse-synthetic-v1-20261109/README.md). Его поля не заполнены и результаты человека не выдумывались. Ни synthetic F1, ни измерение одного локального запуска не доказывают производственную готовность или экономический эффект.
