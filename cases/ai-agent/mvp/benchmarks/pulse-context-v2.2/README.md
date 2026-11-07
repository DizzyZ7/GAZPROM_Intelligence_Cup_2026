# Разделение операций входа и сброса пароля

Версия `context-v2.2` различает `login` и `password_reset` в сценарии `account_access`. Операция определяется по фрагменту с наблюдаемым сбоем. Для пожеланий используется явно указанная операция. Упоминания, исключенные самим автором обращения, отбрасываются. Код HTTP остается признаком симптома. [Код изменения](https://github.com/DizzyZ7/GAZPROM_Intelligence_Cup_2026/commit/cc8f6709cb949d0a33f248014a0ad2494da45c94)

## Проверка поведения

Прежняя версия могла объединить обращения «При входе в кабинет открывается страница с кодом 404» и «При сбросе пароля открывается страница с кодом 404». В новой версии два подтверждающих обращения о входе образуют одну группу, а два обращения о сбросе пароля — другую.

Проверены отрицания «не при входе», «не связано со сбросом пароля», «сброс пароля тут ни при чем», имя учетной записи в выражении «логин правильный» и слово «входящее» в описании письма. При недостаточном контексте возвращается `missing_operation`, при двух наблюдаемых операциях — `ambiguous_operation`.

Через API экспериментального режима проверены формирование двух карточек и повторный импорт с нулем новых карточек. Автоматические регрессии включают пожелания предпросмотра на экране входа и при сбросе пароля. [Тесты](https://github.com/DizzyZ7/GAZPROM_Intelligence_Cup_2026/blob/cc8f6709cb949d0a33f248014a0ad2494da45c94/cases/ai-agent/mvp/tests/test_contextual_analysis.py)

## Оценка на development и validation

Код зафиксирован до прогона. Использованы знакомые выборки набора `pulse-synthetic-v1`, 300 обращений development и 100 validation. Эти прогоны проверяют регрессии на уже использованных данных.

| Выборка | Версия | Верные пары из эталона | Ложные пары | Pairwise F1 | Macro F1 категорий |
| --- | --- | ---: | ---: | ---: | ---: |
| development | context-v2.1 | 31 из 537 | 12 | 0,1069 | 0,3131 |
| development | context-v2.2 | 31 из 537 | 12 | 0,1069 | 0,3131 |
| validation | context-v2.1 | 7 из 71 | 2 | 0,1750 | 0,6909 |
| validation | context-v2.2 | 7 из 71 | 2 | 0,1750 | 0,6909 |

Межпродуктовых объединений в этих прогонах нет. Правка закрыла воспроизведенную ошибку разделения операций. Критерий продвижения P3 требует более 7 верных пар на validation при не более 2 ложных. Текущая полнота остается ниже этого порога.

Если операция и описание сбоя находятся в разных фрагментах, анализатор может отказаться от группировки. Переоценка исторического holdout новым анализатором не выполнялась. Для оценки обобщения потребуется новый независимый набор после улучшения полноты на development и validation. [Результаты v2.1](https://github.com/DizzyZ7/GAZPROM_Intelligence_Cup_2026/blob/c3d799bd63c5479d7be255eaa111ed8238aa43b7/cases/ai-agent/mvp/benchmarks/pulse-context-v2/README.md)

## Воспроизведение

Команды выполняются из `cases/ai-agent/mvp` с Python 3.11 или 3.12. Для каждого прогона создается отдельный каталог. Конфигурация хранит отпечатки набора и исходного кода. В отчетах есть раздельные метрики классификации и группировки, предсказания CSV, коды отказов и квитанция завершения.

```bash
for split in development validation
do
  run_dir=$(mktemp -d)
  python -m context_eval freeze --dataset datasets/pulse-synthetic-v1 --split "$split" --config "$run_dir/frozen.json"
  python -m context_eval evaluate --dataset datasets/pulse-synthetic-v1 --config "$run_dir/frozen.json" --output "$run_dir/result"
  python -m context_eval verify --dataset datasets/pulse-synthetic-v1 --output "$run_dir/result"
done
```

[Development JSON](development/summary.json) · [Development CSV](development/comparison.csv) · [Validation JSON](validation/summary.json) · [Validation CSV](validation/comparison.csv)
