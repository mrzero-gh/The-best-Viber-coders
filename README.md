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

Docker:

```
docker build -t audit-gym .
docker run --rm --network none audit-gym
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
