<p align="center">
  <img src="./assets/readme/hero.svg" width="100%" alt="Audit Gym: синтетические компании со скрытыми ошибками учёта и RL-среда, где ИИ-аудитор получает награду только за доказанные находки">
</p>

<p align="center">
  <a href="https://github.com/mrzero-gh/The-best-Viber-coders/actions/workflows/ci.yml"><img src="https://github.com/mrzero-gh/The-best-Viber-coders/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/tests-~850%20passed-2F7D4F" alt="Около 850 тестов">
  <img src="https://img.shields.io/badge/python-3.11%2B-14171A" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/docker-без%20сети-14171A" alt="Docker без сети">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-6B6F73" alt="MIT"></a>
</p>

<p align="center">
  <a href="docs/DESCRIPTION.md"><b>Описание на 1 страницу</b></a> ·
  <a href="docs/design.md">Записка о дизайне</a> ·
  <a href="docs/judge_adapter.md">Адаптер жюри</a> ·
  <a href="docs/contract.md">Контракт</a>
</p>

**Audit Gym** — RL-среда в стиле Gymnasium для обучения ИИ-аудитора финансовых записей. Генератор создаёт месяц жизни вымышленной компании с ошибками учёта. Агент ищет их через инструменты и отправляет выводы с доказательствами. Оценщик сверяет их с закрытым эталоном и выдаёт награду, которую нельзя получить угадыванием.

## Результат: награду нельзя обмануть

<p align="center">
  <img src="./assets/readme/results.svg" width="100%" alt="Средняя награда: Oracle 1.000, Smart Auditor 0.963, Naive Auditor 0.386, Random −0.501, «Пометить всё» −1.000">
</p>

Средняя награда по 4 сценариям `python -m scripts.jury`: чистая компания, одиночные ошибки, совмещённые сценарии, полный комплекс.

| Политика | F1 | Награда | Шаги | Почему так |
|---|---:|---:|---:|---|
| Oracle | 1.000 | 1.000 | 1.0 | знает эталон: задача разрешима |
| Smart Auditor | 1.000 | 0.963 | 19.4 | открывает документы, учитывает часовой пояс и счёт 2900 |
| Naive Auditor | 0.704 | 0.386 | 15.6 | путает законные операции с ошибками |
| Random | 0.250 | −0.501 | 3.0 | F1 > 0 только на чистой компании |
| «Пометить всё» | 0.000 | −1.000 | 2.0 | ловушки есть даже в чистых компаниях |

Проверки как у жюри проходят полностью: 20 векторов matcher и примеры M01–M12, парные проверки P01–P20 (20/20), около 850 тестов в Docker без сети.

## Что внутри эпизода

Баланс Дт = Кт сходится даже после внесения ошибок, поэтому найти их можно только сверкой с первичными документами.

| Ошибка | Как выглядит в данных |
|---|---|
| `duplicate_invoice` | Два одинаковых счёта с разницей в несколько дней, оба проведены |
| `payment_allocation` | Платёж закрывает счёт целиком, а в книге разнесена лишь часть, остаток на 2900 |
| `fx_conversion` | Проводка посчитана по курсу другой даты или искажённому курсу |
| `period_cutoff` | Документ датирован вне отчётного месяца, а проведён внутри |

Ещё есть два **совмещённых сценария** (две ошибки на одних записях) и четыре вида **законных ловушек**, похожих на ошибки:
- `split_payment`: счёт оплачен частями;
- `refund`: возврат;
- `legit_recurring`: одинаковые счета за разные месяцы;
- `late_evening_local`: документ в 23:xx последнего дня по местному времени.

Размер настраивается: 200–2000 проводок и 20–200 документов. Каждый тип ошибки включается отдельно.

## Как устроено

<p align="center">
  <img src="./assets/readme/workflow.svg" width="100%" alt="Генератор создаёт public и private; среда в отдельном процессе отдаёт агенту только public через инструменты; оценщик сравнивает submit с эталоном и возвращает награду">
</p>

- **Генератор** (`generator/`): детерминированный по `seed`; ID генерируются из отдельного потока, поэтому смена ID не меняет содержание.
- **Среда** (`env/`): отдельный процесс `env.server`, протокол по одной JSON-строке на сообщение. Инструменты `list_records`, `get_record`, `get_document`, `calculator` (без `eval`) и `submit`.
- **Оценщик** (`grader/`): максимальное паросочетание выводов и ошибок с точным сравнением ID, валюты и суммы в пределах допуска. Награда `clip(2·F1 − 1 − 0.002·шаги, −1, 1)`.
- **Изоляция:** эталон не попадает в наблюдения, ошибки и траектории. Это проверяют метки-канарейки (`tests/test_leaks.py`).

## Быстрый старт

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
python -m scripts.check            # все тесты одной командой
```

То же в изолированном контейнере (без сети, только чтение, не от root, 2 CPU / 4 ГБ):

```powershell
python -m scripts.check --docker
```

Посмотреть, как работает агент:

```powershell
python -m scripts.replay --episode EP-0HJPAE81   # пошаговый реплей в терминале
python -m scripts.ui --open                      # веб-дашборд на localhost:8080
python -m scripts.demo                           # демо на 3 минуты: тесты, жюри, парные проверки
```

## Проверка по правилам жюри

Подключение по [определениям проверок FinTech](https://genesis-exe.com/fintech-checks.html): [`judge_adapter.py`](judge_adapter.py) с `generate_episode`, `make_env`, `score_episode`.

| Блок жюри | Баллы | Наша проверка |
|---|---:|---|
| Контракт среды C01–C20 | 25 | `tests/test_judge_adapter.py`, `tests/test_isolation_speed.py`, `tests/test_env_server.py` |
| Закрытые эпизоды D01–D20 | 25 | `judge_adapter.generate_episode` + независимый сертификат `grader.certificate` |
| Matcher и награда | 25 | `tests/test_jury_block3.py`: 20 векторов и M01–M12 |
| Парные проверки P01–P20 | 15 | `python -m scripts.pair_check` → 20/20 |
| Инженерия | 10 | `python -m scripts.check --docker` |

```powershell
python judge_adapter.py            # самопроверка адаптера
python -m scripts.pair_check       # перестановка строк и смена ID, как у жюри
```

Ответы на 5 вопросов жюри и галерея атак на награду — в [записке о дизайне](docs/design.md#64-ответы-на-5-вопросов-жюри).

<details>
<summary><b>Все команды</b></summary>

| Команда | Что делает |
|---|---|
| `python -m scripts.check` | Все тесты (`--docker`: в контейнере без сети) |
| `python -m scripts.jury` | Прогон 5 политик с боевым оценщиком, таблица P/R/F1/награда/шаги |
| `python -m scripts.pair_check` | 20 парных проверок как у жюри |
| `python judge_adapter.py` | Самопроверка адаптера жюри |
| `python -m scripts.plot_complexity` | Кривая сложности → `docs/complexity_curve.png` (нужен `pip install -r requirements-plot.txt`) |
| `python -m scripts.test_metamorphic` | Метаморфные проверки инвариантов |
| `python -m scripts.collect_episodes` | Пересборка 10 train + 10 dev эпизодов и траекторий |
| `python -m generator.explain <эпизод>` | История каждой ошибки эпизода |
| `python -m scripts.replay --list` | Список эпизодов в траекториях |
| `python -m scripts.ui --export docs/audit_ui.html` | Автономный HTML-дашборд |

</details>

<details>
<summary><b>Кривая сложности</b></summary>

![F1 политик на пяти уровнях сложности](docs/complexity_curve.png)

| Уровень | Oracle | Smart Auditor | Naive Auditor | «Пометить всё» | Random |
|---|---:|---:|---:|---:|---:|
| 1. Чистая | 1.000 | 1.000 | 1.000 | 0.000 | 1.000 |
| 2. Простая | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 |
| 3. Средняя | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 |
| 4. Сложная | 1.000 | 1.000 | 0.889 | 0.000 | 0.000 |
| 5. Эксперт | 1.000 | 1.000 | 0.900 | 0.000 | 0.000 |

</details>

<details>
<summary><b>Структура репозитория</b></summary>

| Путь | Назначение |
|---|---|
| `generator/` | Компании, 4 типа ошибок, совмещённые сценарии, ловушки, `explain` |
| `env/`, `common/` | Среда `AuditEnv`, сервер, калькулятор |
| `grader/` | Matcher, награда, сертификат разрешимости |
| `policies/` | Oracle, RuleBased (Smart/Naive), FlagAll, Random |
| `schemas/` | JSON-схемы Draft 2020-12 |
| `episodes/`, `trajectories.jsonl` | 10 train + 10 dev эпизодов с отпечатками и траектории политик |
| `scripts/` | Проверка, жюри, парные проверки, демо, реплей, UI |
| `judge_adapter.py` | Интерфейс закрытой проверки жюри |
| `docs/` | Описание, записка о дизайне, контракт, данные |

</details>

## Ограничения

Модель учёта упрощена: нет НДС и пошлин, пять счетов, платёж закрывает счета только в своей валюте. Высокая награда в симуляторе подтверждает корректность расследования, но не пригодность модели для реального аудита без дообучения. Подробно — [docs/design.md, раздел 7](docs/design.md#7-честные-ограничения-системы).

## Документация

- [docs/DESCRIPTION.md](docs/DESCRIPTION.md) — описание проекта на одну страницу
- [docs/design.md](docs/design.md) — записка о дизайне: блоки жюри, matcher, разбор сбоев, атаки, ответы на 5 вопросов
- [docs/judge_adapter.md](docs/judge_adapter.md) — адаптер жюри: форматы, коды ошибок
- [docs/contract.md](docs/contract.md) — контракт среды: протокол, инструменты, лимиты
- [docs/DATA.md](docs/DATA.md) — данные эпизода, `truth.json`, `provenance.json`
- [CONTRIBUTING.md](CONTRIBUTING.md) — работа с Git

Команда **The Best Viber Coders**, Genesis Hackathon 2026. Лицензия [MIT](LICENSE).
