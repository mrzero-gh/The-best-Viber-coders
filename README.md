# Audit Gym — RL-среда для ИИ-аудитора

RL-среда в стиле Gymnasium: агент-аудитор исследует синтетические финансовые данные и сообщает о найденных нарушениях.
Оценщик сравнивает находки с эталоном (F1) и выдаёт награду; агент не видит private/.

## Быстрый старт

```powershell
# Python 3.11+
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
python -m scripts.check
```

Docker — проверка как у жюри, одной командой:

```
python -m scripts.check --docker
```

Собирает образ (Python 3.11.18, базовый образ закреплён по digest) и запускает все тесты в контейнере без сети, с файловой системой только для чтения, не от root, с ограничением 2 CPU / 4 ГБ. Внутри дополнительно проверяется, что сети действительно нет. То же вручную:

```
docker build -t audit-gym .
docker run --rm --network none --read-only --tmpfs /tmp --cpus 2 --memory 4g -e AUDIT_GYM_EXPECT_ISOLATION=1 audit-gym
```

## Структура

| Папка / файл | Назначение | Владелец |
|---|---|---|
| `generator/` | генератор эпизодов | A |
| `env/`, `common/`, `schemas/`, `docs/contract.md` | среда, общие утилиты, контракт | B |
| `grader/` | сопоставление и награда | C |
| `policies/`, `scripts/`, `episodes/` | политики, скрипты, эпизоды | D |

## Документы

- [docs/contract.md](docs/contract.md) — контракт среды (форматы, действия, награда)
- [docs/DATA.md](docs/DATA.md) — что генерируется и что выходит, с примерами
- [docs/TEAM-PLAN.md](docs/TEAM-PLAN.md) — план и роли
- [docs/IDEAS.md](docs/IDEAS.md) — идеи сверх требований кейса
- [CONTRIBUTING.md](CONTRIBUTING.md) — как работать с Git
