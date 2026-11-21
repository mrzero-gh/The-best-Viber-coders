# Данные Audit Gym: что генерируется и что выходит

Короткий справочник для всех, кто пишет политики, скрипты и отчёты (в первую очередь D). Точные правила — в [contract.md](contract.md), машинная проверка — в [schemas/](../schemas). Все примеры ниже взяты из настоящего эпизода: `seed = 42`, конфиг по умолчанию.

## 1. Поток данных

```
seed + config ──► генератор (A) ──► episodes/<split>/<EP-…>/
                                      ├─ public/   6 файлов: записи компании (видит агент через инструменты)
                                      └─ private/  truth, provenance, generation (агент НЕ видит)
                                                │
                     ┌──────────────────────────┘
                     ▼
политика ──action──► env.server (B) ──observation──► политика
   ▲                    │  при submit: findings ──► оценщик (C) ──► reward + metrics
   │                    └────────► runs/<run_id>/grader_log.jsonl (журнал оценщика)
   └── пишет trajectories.jsonl (логгер D)
```

| Кто | Получает | Отдаёт |
|---|---|---|
| Генератор | `seed`, `config` | папку эпизода `public/` + `private/` |
| Среда | эпизод (сама генерирует по `seed`+`config` или берёт `--episode`) | наблюдения агенту, findings оценщику, строку журнала |
| Оценщик | findings агента + `truth` | `valid`, `reward`, `metrics` |
| Политика | наблюдения | действия, в конце `submit` с findings |

## 2. Вход: seed и config

- `seed` — целое число. Тот же `(seed, config)` всегда даёт побайтно тот же эпизод (проверено тестами, задача #12).
- `config` — словарь, схема `config.schema.json`, полный список полей — контракт, раздел 13. Значения по умолчанию: `generator.config.DEFAULT_CONFIG`.

```json
{
  "config_version": "default-v1", "mode": "train",
  "company": {"archetype": "importer", "base_currency": "KZT", "timezone": "+05:00",
              "foreign_currencies": ["USD", "CNY", "EUR", "RUB", "JPY"]},
  "size": {"ledger_rows": [300, 1200], "documents": [40, 120]},
  "errors": {"duplicate_invoice": {"enabled": true, "count": [0, 2]}, "...": "так же для 4 типов",
             "payment_allocation": {"enabled": true, "count": [1, 3], "variant": "any"}},
  "interactions": {"partial_fx_payment_at_cutoff": {"enabled": false, "count": [0, 1]}, "...": "..."},
  "distractors": {"split_payment": {"enabled": true, "count": [0, 2]}, "...": "..."},
  "clean": false, "max_errors": 12
}
```

`mode` (`train` / `eval`) генератор не использует — его читает среда.

`errors.payment_allocation.variant` (необязательно): `any` (по умолчанию: в книге не разнесено ничего в 1 случае из 4, иначе 20–80 % суммы), `missing` (всегда пропуск: ничего не разнесено), `partial` (всегда частичное распределение). Так отдельно получаются эпизоды D09–D11 жюри. Совмещённый сценарий `partial_fx_payment_at_cutoff` вариант не меняет.

## 3. Что генерируется: папка эпизода

Сгенерировать эпизод на диск: `python -m generator.build --seed 42 --out episodes/dev [--config cfg.json]`.

### 3.1. `public/` — данные компании (агент видит их только через инструменты)

**`manifest.json`** — паспорт эпизода, агент получает его целиком в первом наблюдении.
```json
{"episode_id": "EP-11N2H5M6", "base_currency": "KZT", "timezone": "+05:00",
 "period_start": "2026-11-01T00:00:00+05:00", "period_end": "2026-12-01T00:00:00+05:00",
 "counts": {"ledger_rows": 832, "invoices": 59, "payments": 41, "documents": 100, "fx_rates": 255},
 "currency_exponents": {"KZT": 2, "USD": 2, "EUR": 2, "CNY": 2, "RUB": 2, "JPY": 0},
 "rounding": {"mode": "ROUND_HALF_EVEN"}, "csv_null": "", "config_version": "default-v1", "schema_version": "1.0"}
```

**`policy.json`** — правила учёта, тоже приходит в первом наблюдении: курс берётся на локальную дату документа, `1 source = rate target`, округление после пересчёта, период `[start, end)`, допуск по сумме по типам (`amount_tolerance_minor_by_type`: 0 для `duplicate_invoice` и `period_cutoff`, 1 для `fx_conversion` и `payment_allocation`).

**`ledger.csv`** — книга проводок (таблица `ledger`).
```
row_id,timestamp,account,debit_minor,credit_minor,currency,entity_id,invoice_id,payment_id
L-A4K0WW4K,2026-10-10T09:25:00+05:00,4000,0,20111600,KZT,E-JVBCY9G9,INV-4CX3MJK6,
```
Счета: `1010` деньги, `1200` дебиторская задолженность, `2900` невыясненные поступления, `4000` выручка, `7900` курсовые разницы. Как проводятся счёт и платёж — контракт, раздел 4. Суммы — целые минимальные единицы базовой валюты (`20111600` = 201 116,00 KZT). Пустая ячейка в CSV = `null` в ответе среды.

**`invoices.jsonl`** — счета (таблица `invoices`), одна JSON-строка на счёт.
```json
{"invoice_id": "INV-4CX3MJK6", "doc_id": "DOC-F7XDQ4N6", "entity_id": "E-JVBCY9G9", "currency": "KZT",
 "issued_at": "2026-10-10T09:25:00+05:00", "due_at": "2026-11-09T09:25:00+05:00", "total_minor": 83495900,
 "line_items": [{"description": "Смартфоны, октябрь 2026", "quantity": 7, "unit_price_minor": 9054900, "amount_minor": 63384300}, "..."]}
```
`line_items` видны только через `get_document`; `list_records` и `get_record` их не отдают.

**`payments.jsonl`** — платежи (таблица `payments`). Платёж закрывает только счета в своей валюте.
```json
{"payment_id": "PAY-W3DW9N50", "doc_id": "DOC-B4Y2EF36", "timestamp": "2026-11-06T17:40:00+05:00",
 "currency": "JPY", "amount_minor": 1947184,
 "invoice_refs": [{"invoice_id": "INV-R9ZFMG4H", "amount_minor": 1947184}]}
```

**`fx.csv`** — курсы (таблица `fx`), на каждый день от `period_start − 10` до `period_end + 10` дней.
```
date,source_currency,target_currency,rate
2026-10-22,CNY,KZT,65.0520
```
Курс — десятичная строка, не float. ID курса для `get_record` и `evidence_ids`: `FX:2026-10-22:CNY:KZT`.

**Форматы везде:** ID — 8 символов base32 Крокфорда с префиксом (`EP-`, `L-`, `INV-`, `PAY-`, `DOC-`, `E-`, `ISS-`, `DST-`); время — ISO-8601 со смещением; деньги — целые minor units; курсы — строки.

### 3.2. `private/` — эталон (агент не видит никогда)

| Файл | Что внутри | Кто читает |
|---|---|---|
| `truth.json` | `issues[]` — ошибки: `issue_id`, `type`, `affected_ids`, `expected_amount_minor`, `currency`, `amount_tolerance_minor`, `evidence_sets[]`, `scenario`; `distractors[]` — законные «ловушки» | оценщик, oracle, отчёты |
| `provenance.json` | как вносились ошибки (`mutations`), хэш чистой истории; формат в разделе 3.3 | разбор сбоев, `generator.explain` |
| `generation.json` | `seed`, `config`, версия генератора, `public_sha256` и `episode_sha256` (отпечаток), запланировано/внесено ошибок и ловушек | среда (проверка отпечатка), отчёты |

Пример ошибки из `truth.json`:
```json
{"issue_id": "ISS-FA0Y0KMS", "type": "period_cutoff", "affected_ids": ["INV-EB1XPBJB"],
 "expected_amount_minor": 2076280, "currency": "JPY", "amount_tolerance_minor": 0,
 "evidence_sets": [["DOC-69MP4DT0", "L-135CK00B"], ["DOC-69MP4DT0", "L-1FBEEYHS"], "..."], "scenario": null}
```

### 3.3. Формат `provenance.json`

Журнал того, как чистая история компании превратилась в эпизод с ошибками. По нему видно, какое изменение породило каждую ошибку из `truth.json`.

```json
{"episode_id": "EP-…", "generator_version": "0.6.0",
 "clean_history_sha256": "9f…",
 "mutations": [
   {"issue_id": "ISS-…", "type": "fx_conversion",
    "changes": [{"table": "fx", "record_id": "FX:2026-05-11:JPY:KZT", "field": "rate_used",
                 "before": "3.0776", "after": "3.0865 (rate_of:2026-05-12)"}, "…"]}]}
```

| Поле | Значение |
|---|---|
| `episode_id`, `generator_version` | как в `generation.json` |
| `clean_history_sha256` | sha256 чистой истории **до** внесения ошибок, но уже с отвлекающими случаями (`generator.build.history_digest`: канонический JSON таблиц ledger, invoices, payments, fx) |
| `mutations[]` | одна запись на каждую ошибку, в том же порядке, что `truth.issues`; `issue_id` и `type` совпадают с `truth.json` |
| `changes[]` | `{table, record_id, field, before, after}`: что именно изменилось |
| `clean_history_diff` | полный обратимый журнал отличий открытых таблиц от чистой истории (ниже) |

Отвлекающие случаи (`truth.distractors`) в `mutations` не попадают: это законные данные чистой истории.

**Значения `field`.** Обычное имя поля означает правку значения. Имена с `__` и с двоеточием служебные.

| `type` | `table` | `field` | `record_id` | `before` → `after` |
|---|---|---|---|---|
| `duplicate_invoice` | `invoices` | `__copy_of__` | ID дубликата | `null` → ID оригинала |
| любой | `ledger` | `__created__` | `row_id` новой проводки | `null` → ID документа, к которому она относится |
| любой, кроме дубликата | `ledger` | `__deleted__` | `row_id` удалённой проводки | ID документа → `null` |
| `payment_allocation` | `ledger` | `allocated_minor:<INV>` | ID платежа | сколько должно было разнестись на счёт `<INV>` → сколько разнесено (minor) |
| `fx_conversion` | `ledger` | `debit_minor` | `row_id` ключевой проводки (1200 для счёта, 1010 для платежа) | верная сумма в базовой валюте → проведённая |
| `fx_conversion` | `fx` | `rate_used` | ключ курса `FX:<дата>:<валюта>:<база>` | верный курс → `"<курс> (rate_of:<другая дата>)"` или `"<курс> (distorted:<доля>)"` |
| `period_cutoff` | `invoices` / `payments` | `issued_at` / `timestamp` | ID документа | исходная дата → дата за границей периода |
| `period_cutoff` | `ledger` | `posted_at` | ID документа (все его проводки) | исходная дата → дата проведения |

Документ перепроводится целиком: у `fx_conversion` и `period_cutoff` старые проводки документа идут как `__deleted__`, новые как `__created__`. Сумма ошибки (`expected_amount_minor` в `truth.json`) следует из `before`/`after`: у `fx_conversion` это `|after − before|` поля `debit_minor`, у `payment_allocation` это `before − after` поля `allocated_minor`.

**Сценарии взаимодействия** (`truth.issues[].scenario`) дают несколько записей в `mutations`, по одной на каждую ошибку сценария. Изменения одной записи могут затрагивать проводки, созданные предыдущей (например, `__deleted__` строки, которую создал дубликат).

**Журнал `clean_history_diff`.** `mutations` объясняет каждую ошибку, но описательно: в нём есть служебные поля (`allocated_minor:<INV>`, `rate_used`), а у удалённых проводок записан только ключ документа. Для точного восстановления есть отдельный полный журнал: генератор сравнивает таблицы до и после внесения ошибок.

```json
"clean_history_diff": {
  "ledger":   {"removed": [{"row_id": "L-…", "timestamp": "…", "account": "1200", "debit_minor": 125000,
                            "credit_minor": 0, "currency": "KZT", "entity_id": "ENT-…",
                            "invoice_id": "INV-…", "payment_id": null}],
               "added": ["L-…"], "modified": []},
  "invoices": {"removed": [], "added": ["INV-…"],
               "modified": [{"id": "INV-…", "field": "issued_at", "before": "…", "after": "…"}]},
  "payments": {"removed": [], "added": [], "modified": []},
  "fx":       {"removed": [], "added": [], "modified": []}}
```

| Ключ | Значение |
|---|---|
| `removed` | строки чистой истории целиком, в формате открытых файлов; в открытых данных их нет |
| `added` | ID строк, которых не было в чистой истории (у курса ID - `FX:<дата>:<валюта>:<база>`) |
| `modified` | `{id, field, before, after}`: `after` равен текущему открытому значению |

Чистая история = открытые таблицы, из которых убраны `added`, возвращены `removed`, а у `modified` поставлено `before`. Строки сортируются как в открытых файлах (время, затем ID; курсы - дата и валюты), sha256 канонического JSON четырёх таблиц равен `clean_history_sha256`. У чистой компании журнал пустой.

**Как проверить независимо.**
- `python -m generator.explain <папка эпизода> [ISS-…]` рассказывает историю каждой ошибки по `provenance.json`.
- `python -m grader.clean_history <папка эпизода>` восстанавливает чистую историю по открытым данным и `clean_history_diff` и проверяет три вещи: sha256 совпадает с `clean_history_sha256`; в книге до внесения ошибок дебет равен кредиту и сходится каждый документ; журнал согласован с открытыми данными. Эту же проверку делает сертификат `grader.certificate` (пункт `clean_history_balance`).
- `python -m grader.consistency <папка эпизода>` проверяет, что чистая история верна по правилам учёта (каждый документ проведён по своей дате, курсу и распределениям выписки; каждое ненулевое распределение видно строкой Кт 1200; дублей счетов нет; курсы не менялись) и что каждое отличие открытых данных от неё покрыто ошибкой эталона своего типа. Это пункт `clean_history_rules` сертификата.
- Перегенерация тоже работает: тот же `seed` и `config` из `generation.json` дают тот же эпизод и тот же `clean_history_sha256`.
- `record_id` у `__deleted__` в открытых данных нет. Остальные `record_id` в открытых данных есть. Исключение: `__created__`-строки дубликата в сценарии `duplicate_invoice_wrong_fx`, которые следующая запись удаляет как `__deleted__`.

> Эпизоды в `episodes/` руками не править: среда сверяет sha256 и отклонит такой эпизод (`EPISODE_INVALID`). Нужно другое — перегенерировать.

## 4. Что видит агент

```python
from env.client import AuditEnv
with AuditEnv() as env:                                   # сам генерирует по seed+config
# with AuditEnv(episode="episodes/dev/EP-11N2H5M6") as env:  # или готовый эпизод с диска
    obs, info = env.reset(42, config)                      # config["mode"] = "train" или "eval"
    obs, reward, terminated, truncated, info = env.step({"tool": "list_records", "args": {"table": "ledger"}})
```

**Первое наблюдение** (`kind: "initial"`): `task` (текст задания), `manifest`, `policy`, `tools`, `tables`, `steps_used: 0`, `steps_remaining: 100`.

**Действия** — ровно `{"tool": ..., "args": {...}}`:

| tool | args | Что возвращает в `result` |
|---|---|---|
| `list_records` | `table`, необяз. `cursor`, `limit` ≤ 50 | `records[]`, `next_cursor` (`"c50"` или `null`), `total` |
| `get_record` | `table`, `id` | `record` |
| `get_document` | `doc_id` | `document` + `doc_type` (`invoice` / `payment`), у счёта есть `line_items` |
| `calculator` | `expression` ≤ 200 символов: числа, `+ - * / ( )` | `value` — десятичная строка, например `"587812.5"` |
| `submit` | `findings` | завершает эпизод |

**Наблюдения** бывают трёх видов:
```json
{"kind": "result", "tool": "list_records", "result": {"table": "ledger", "records": ["..."], "next_cursor": "c2", "total": 832}, "steps_used": 1, "steps_remaining": 99}
{"kind": "error",  "tool": "get_record",   "error": {"code": "NOT_FOUND", "message": "Запись не найдена: L-XXXXXXXX"}, "steps_used": 4, "steps_remaining": 96}
{"kind": "final",  "status": "submitted", "steps_used": 5, "steps_remaining": 95}
```
Коды ошибок инструментов: `INVALID_ACTION`, `UNKNOWN_TOOL`, `UNKNOWN_TABLE`, `INVALID_ARGS`, `INVALID_CURSOR`, `NOT_FOUND`, `CALC_ERROR`, `EPISODE_FINISHED`. Ошибочный шаг тоже тратит бюджет. Статусы `final`: `submitted`, `invalid_findings`, `step_limit` (100 шагов), `time_limit` (60 с).

**`info`**: всегда `episode_id` и `mode`. В `train` на последнем шаге ещё `metrics`. Ошибки протокола (`BAD_REQUEST`, `BAD_CONFIG`, `NO_EPISODE`, `GENERATION_FAILED`, `EPISODE_INVALID`, `INTERNAL`) приходят исключением `env.client.EnvError` с полем `code`.

## 5. Что агент отправляет: findings

```json
{"episode_id": "EP-11N2H5M6", "findings": [
  {"type": "payment_allocation", "affected_ids": ["INV-3HZ7QK2D", "PAY-9F4TX1MB"],
   "claimed_amount_minor": 125000, "currency": "USD",
   "evidence_ids": ["DOC-5RW2N8CA", "L-0Q7M2ZK4", "L-7TBC4X1P"],
   "explanation": "Платёж закрывает счёт целиком, в книге разнесена половина."}
]}
```
Не больше 30 выводов, 10 `evidence_ids` на вывод и 1000 символов в `explanation`. Пустой список `findings` — честный ответ «ошибок нет». Что считается верным для каждого типа — контракт, раздел 9. Схема — `findings.schema.json`.

## 6. Что выходит

**Награда** (только при завершении): `clip(2·F1 − 1 − 0.002·inspection_steps, −1, 1)`, где `inspection_steps` — шаги до `submit`. Неверная структура findings → −1.

| | `train` | `eval` |
|---|---|---|
| `reward` политике | настоящая, например `0.992` | всегда `0.0` |
| `info.metrics` | `{"tp": 4, "fp": 0, "fn": 0, "precision": 1.0, "recall": 1.0, "f1": 1.0}` | нет |
| журнал оценщика | пишется | пишется (только там видна награда) |

**Журнал оценщика** `runs/<run_id>/grader_log.jsonl` — строка на эпизод, схема `grader_log.schema.json`. Отсюда `scripts.jury` и таблицы берут результаты eval.
```json
{"run_id": "demo", "episode_id": "EP-11N2H5M6", "mode": "train", "seed": 42, "config_version": "default-v1",
 "status": "submitted", "steps_used": 5, "inspection_steps": 4, "reward": 0.992, "valid": true, "error": null,
 "metrics": {"tp": 4, "fp": 0, "fn": 0, "precision": 1.0, "recall": 1.0, "f1": 1.0},
 "episode_sha256": "7a893d52…"}
```
Путь задаётся при создании среды: `AuditEnv(run_id="baseline-1", runs_dir="runs")`. Папка `runs/` в git не попадает.

**Траектории** `trajectories.jsonl` пишет логгер политики (D) на стороне клиента — строка на шаг, ровно 7 полей, схема `trajectory.schema.json`:
```json
{"episode_id": "EP-11N2H5M6", "step": 0, "action": null, "observation": {"kind": "initial", "...": "..."}, "reward": 0.0, "terminated": false, "truncated": false}
{"episode_id": "EP-11N2H5M6", "step": 1, "action": {"tool": "list_records", "args": {"table": "ledger"}}, "observation": {"kind": "result", "...": "..."}, "reward": 0.0, "terminated": false, "truncated": false}
```
В траектории попадает только то, что вернула среда, поэтому эталона в ней нет.

## 7. Схемы и проверки

| Схема | Что описывает |
|---|---|
| `config.schema.json` | конфиг генератора |
| `manifest.schema.json`, `records.schema.json` | `public/`: паспорт, проводки, счета, платежи, курсы, правила |
| `truth.schema.json` | `private/truth.json` |
| `action.schema.json`, `observation.schema.json` | действия и наблюдения |
| `findings.schema.json` | ответ агента |
| `trajectory.schema.json` | строка `trajectories.jsonl` |
| `grader_log.schema.json` | строка журнала оценщика |

`tests/test_generated_schemas.py` проверяет, что настоящие эпизоды генератора, ответы среды и строки журнала проходят эти схемы. Если формат поменяется, тест упадёт.
