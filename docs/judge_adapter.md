# Адаптер жюри (`judge_adapter.py`)

Этот документ описывает подключение по разделу «Единый способ подключения» на странице [определений проверок FinTech](https://genesis-exe.com/fintech-checks.html).

- **Рабочая директория:** корень репозитория. В Docker-образе это `/app`.
- **Импорт:** `import judge_adapter`.
- **Самопроверка одной командой:** `python judge_adapter.py`. Она генерирует эпизод, проходит его в закрытом режиме, вызывает `score_episode` и печатает итог в JSON.
- **Тесты адаптера:** `tests/test_judge_adapter.py`, сценарии C01–C20, `score_episode` и парные проверки. Они входят в `python -m scripts.check` и в `python -m scripts.check --docker`.

## Точки входа

| Функция | Что делает |
|---|---|
| `generate_episode(seed, config, output_dir)` | Обёртка над `generator.build.generate_episode`. Пишет эпизод прямо в `output_dir`: файлы `public/` (`manifest.json`, `ledger.csv`, `invoices.jsonl`, `payments.jsonl`, `fx.csv`, `policy.json`) и закрытые `private/` (`truth.json`, `provenance.json`, `generation.json`). `config` — это наш конфиг генератора (см. [DATA.md](DATA.md)), `mode: "closed"` в нём понимается как `eval`. Одинаковые seed и config дают побайтно одинаковые `public/`. |
| `make_env()` | Возвращает объект `JudgeEnv` с методами `reset(seed, config)`, `step(action)` и `close()`. |
| `score_episode(episode_dir, findings, inspection_steps)` | Закрытый вызов оценщика `grader.evaluate`. Возвращает `{"tp","fp","fn","precision","recall","f1","return"}`. Для невалидной структуры возвращается `return = -1`, остальные поля равны `null`. Каждый вызов (вместе с ошибкой) пишется в `<runs>/judge-scores/score_log.jsonl`. |

## `reset(seed, config)`

- `{"mode": "closed", "episode_dir": DIR, "team_config": {...}}`. Загружается ровно этот эпизод: ничего не генерируется, ID не меняются. Повторный `reset` начинает тот же эпизод заново.
- `{"mode": "train", "team_config": {...}}` без `episode_dir`. Генератор команды получает `seed` и `team_config` и создаёт эпизод во временной папке. Папка удаляется сразу после загрузки.
- Каждый `reset` запускает новый процесс `python -m env.server`, поэтому состояние прошлого эпизода не сохраняется.
- Лимит 60 секунд считается от `reset`, включая время политики.
- Первое наблюдение: `{"task", "manifest", "policy", "remaining_steps": 100}`.
- `info` всегда равен `{"episode_id", "mode"}`, где `mode` — это `closed` или `train`.

## `step(action)`

Действие передаётся как `{"name": <действие>, "args": {...}}`. Адаптер переводит его в наш формат `{"tool", "args"}`. Любое другое значение уходит в среду как есть: она ответит `INVALID_ACTION` и израсходует шаг.

| Действие | args | `result` в наблюдении |
|---|---|---|
| `list_records` | `table`, `cursor` (первый — `null`), `limit` (1..50, по умолчанию 50) | `{"records": [...], "next_cursor": str \| null}` |
| `get_record` | `table`, `id` | объект записи |
| `get_document` | `doc_id` | объект документа с полем `doc_type` |
| `calculator` | `expression` | десятичная строка, например `"0.3"` |
| `submit` | `findings` (объект findings.json) | `{"accepted": true}` |

- **Таблицы:** `ledger` (ID в поле `row_id`), `invoices` (`invoice_id`), `payments` (`payment_id`), `fx` (ID вида `FX:<date>:<source_currency>:<target_currency>`). Поля записей описаны в [DATA.md](DATA.md). В `list_records` и `get_record` у счетов нет `line_items`: полный документ отдаёт `get_document` по `doc_id`.
- **Формат наблюдения после шага:** `{"result": value | null, "error": null | {"code", "message"}, "remaining_steps": int}`.
- **Публичные коды ошибок:**
  - `INVALID_ACTION`, `UNKNOWN_TOOL`, `UNKNOWN_TABLE`, `INVALID_ARGS`, `INVALID_CURSOR`, `NOT_FOUND`, `CALC_ERROR`, `EPISODE_FINISHED`;
  - при завершении: `INVALID_FINDINGS` (ошибка структуры submit, без оценки правильности), `STEP_LIMIT`, `TIME_LIMIT`.
- **Награда:** в `closed` она всегда `0.0`. В `train` терминальная награда приходит на завершающем шаге. Метрики оценщика политике не передаются ни в одном режиме.
- **Счёт шагов:** submit входит в лимит 100 действий, но не входит в `inspection_steps`; ошибочные действия входят в оба счётчика. Submit на действии 100 принимается. 100 действий без submit дают `truncated=true` и пустые findings.

## Типы ошибок

| `type` в findings | Что нарушено | Канонические `affected_ids` | Сумма и валюта |
|---|---|---|---|
| `duplicate_invoice` | Одно обязательство отражено дважды (копия счёта проведена повторно) | оба `invoice_id`, отсортированы | `total_minor` дубля, валюта счёта |
| `payment_allocation` | Платёж закрывает счёт целиком, а в книге распределена только часть или ничего | `[invoice_id, payment_id]`, отсортированы | нераспределённая сумма, валюта платежа |
| `fx_conversion` | Проводка в KZT посчитана по курсу другой даты или по искажённому курсу | `[invoice_id или payment_id]` | разница в базовой валюте |
| `period_cutoff` | Документ с локальной датой вне периода проведён внутри периода | `[invoice_id или payment_id]` | сумма и валюта документа |

Допустимые наборы доказательств описаны в [contract.md](contract.md), раздел 9. ID курса имеет вид `FX:<дата>:<валюта>:<база>`.

- **Сценарии взаимодействия** (включаются в `config.interactions`): `partial_fx_payment_at_cutoff` и `duplicate_invoice_wrong_fx`.
- **Законные отвлекающие случаи** (`config.distractors`): `split_payment`, `refund`, `legit_recurring`, `late_evening_local`.
- Каждый тип ошибки включается отдельно через `config.errors.<type>.enabled` и `count`.
- Допуск суммы задан в `policy.amount_tolerance_minor` (сейчас 1 минимальная единица) и в `truth` для каждой ошибки.

## Изоляция и парные проверки

- **Отдельный процесс.** Политика работает в процессе жюри и видит только наблюдения. Каталог эпизода, `truth` и оценщик доступны только серверному процессу и `score_episode`.
- **Отпечаток эпизода.** Сервер сверяет хэши `public/` с `private/generation.json` (контракт, раздел 15). Адаптер запускает сервер с флагом `--trust-episode`, потому что жюри в парных проверках само переставляет записи и меняет ID в копии эпизода. В этом режиме отпечаток не сверяется, а пересчитывается по фактическим файлам и пишется в журнал оценщика. Без флага (в собственных запусках команды) изменённая копия по-прежнему отклоняется с кодом `EPISODE_INVALID`.
