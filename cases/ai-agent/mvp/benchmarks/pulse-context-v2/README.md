# Проверка контекстного разбора v2

Продолжение работы с разделением операций входа и сброса пароля доступно в отчете v2.2. [Отчет v2.2](../pulse-context-v2.2/README.md)

Контекстный разбор группирует обращения по продукту, категории, сценарию, операции и симптому. Минимальный размер группы равен двум обращениям. Оба метода запускаются локально без эмбеддингов.

В `context-v2` добавлены формулировки для числовых кодов, сброса пароля и повторных платежей. Проверка на отдельных синтетических примерах выявила ложные срабатывания для счетчика заявок, условного вопроса и рассрочки. В `context-v2.1` числовой код требует признака ответа страницы или ошибки, а два платежа рассматриваются как дубликат при явном указании одного подтверждения. Восстановлены явные просьбы сбросить пароль. [Код v2](https://github.com/DizzyZ7/GAZPROM_Intelligence_Cup_2026/commit/c39fafec72ce9d98cc9f65956a3e5e319bfccb3a) · [Код v2.1](https://github.com/DizzyZ7/GAZPROM_Intelligence_Cup_2026/commit/25352e225f082339be8ec74328c8014e20eb67cb)

## Результаты

Прогоны используют существующие выборки набора `pulse-synthetic-v1`. Код каждой версии зафиксирован перед запуском. Предсказания каждого метода вычисляются до чтения эталонных меток. Проверка v2.1 на validation повторно использует ту же выборку после исправлений по синтетическим примерам.

| Выборка | Метод | Верные пары из эталона | Ложные пары | Pairwise F1 | Macro F1 категорий |
| --- | --- | ---: | ---: | ---: | ---: |
| development | rules | 130 из 537 | 357 | 0,2539 | 0,2009 |
| development | context-v1 | 28 из 537 | 12 | 0,0971 | 0,2995 |
| development | context-v2 | 43 из 537 | 12 | 0,1453 | 0,3186 |
| development | context-v2.1 | 31 из 537 | 12 | 0,1069 | 0,3131 |
| validation | rules | 0 из 71 | 1 | 0,0000 | 0,0688 |
| validation | context-v1 | 7 из 71 | 2 | 0,1750 | 0,6909 |
| validation | context-v2 | 7 из 71 | 2 | 0,1750 | 0,6909 |
| validation | context-v2.1 | 7 из 71 | 2 | 0,1750 | 0,6909 |

Критерий перехода к новой контрольной оценке требовал более 7 верных пар, не более 2 ложных и 0 межпродуктовых объединений на validation. Обе новые версии остановились на критерии полноты. Счетчик `resolved_gold_pairs` показывает число эталонных пар, у которых извлечен полный контекст для обоих обращений. В этих прогонах все такие пары попали в группы. Основные пропуски происходят до сравнения обращений.

На синтетических примерах сообщение о странице с кодом 404 при сбросе пароля может получить тот же ключ `account_access/http_404`, что и ошибка входа. Это происходит, когда текст о сбросе не содержит признака канала восстановления или кода сброса. Для разделения этих случаев требуется извлекать операцию.

Из шести размеченных попыток вложить инструкции на validation контекстные правила отметили пять. Это проверка текстовой эвристики на синтетическом наборе. Сохраненные результаты контрольной выборки v1 относятся к зафиксированной ревизии. Версии v2 и v2.1 на этой выборке не запускались. [Контрольный отчет v1](https://github.com/DizzyZ7/GAZPROM_Intelligence_Cup_2026/blob/b3dc5e8a82250aa5b2d4b1a6dd73cad0bf96a216/cases/ai-agent/mvp/benchmarks/pulse-context-holdout-v1/README.md)

## Воспроизведение

Выполните команды в `cases/ai-agent/mvp` с Python 3.11 или 3.12. Для каждого прогона создается отдельный каталог. В отчете сохраняются отпечатки кода, манифеста набора и файлов результатов.

```bash
for split in development validation
do
  run_dir=$(mktemp -d)
  python -m context_eval freeze --dataset datasets/pulse-synthetic-v1 --split "$split" --config "$run_dir/frozen.json"
  python -m context_eval evaluate --dataset datasets/pulse-synthetic-v1 --config "$run_dir/frozen.json" --output "$run_dir/result"
  python -m context_eval verify --dataset datasets/pulse-synthetic-v1 --output "$run_dir/result"
done
```

Для сравнения с `context-v1` подготовьте временную копию зафиксированной ветки и используйте тот же оценщик. Команды выше повторите из каталога временной копии. Метрики качества детерминированы, время анализа зависит от среды.

```bash
repo_root=$(git rev-parse --show-toplevel)
baseline_dir=$(mktemp -d)
git -C "$repo_root" archive b3dc5e8a82250aa5b2d4b1a6dd73cad0bf96a216 -- cases/ai-agent/mvp | tar -xf - -C "$baseline_dir"
cp context_eval.py "$baseline_dir/cases/ai-agent/mvp/"
cd "$baseline_dir/cases/ai-agent/mvp"
```

[Development JSON](development/summary.json) · [Development CSV](development/comparison.csv) · [Validation JSON](validation/summary.json) · [Validation CSV](validation/comparison.csv)

Сравнения и отпечатки первого прогона сохранены отдельно. Для воспроизведения используйте код v2 и конфигурацию выбранной выборки. [Первый прогон development](initial/development) · [Первый прогон validation](initial/validation)
