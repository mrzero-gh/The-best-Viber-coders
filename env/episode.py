"""Загрузка эпизода с диска: открытая часть (public/) и эталон (private/truth.json)."""
import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from common.jsonio import read_jsonl

# Первичные ключи таблиц (fx — составной, см. fx_key)
PRIMARY_KEYS = {"ledger": "row_id", "invoices": "invoice_id", "payments": "payment_id"}


def fx_key(row: dict) -> str:
    """Первичный ключ строки курса: FX:<date>:<source>:<target>."""
    return f"FX:{row['date']}:{row['source_currency']}:{row['target_currency']}"


@dataclass
class Episode:
    """Открытые данные эпизода + индексы для быстрого поиска."""
    manifest: dict
    policy: dict
    ledger: list
    invoices: list
    payments: list
    fx: list
    # records[table][pk] -> строка; documents[doc_id] -> (doc_type, строка)
    records: dict = field(default_factory=dict)
    documents: dict = field(default_factory=dict)

    def __post_init__(self):
        self.records = {
            "ledger": {r["row_id"]: r for r in self.ledger},
            "invoices": {r["invoice_id"]: r for r in self.invoices},
            "payments": {r["payment_id"]: r for r in self.payments},
            "fx": {fx_key(r): r for r in self.fx},
        }
        self.documents = {r["doc_id"]: ("invoice", r) for r in self.invoices}
        self.documents.update({r["doc_id"]: ("payment", r) for r in self.payments})

    @property
    def episode_id(self) -> str:
        return self.manifest["episode_id"]

    def public_ids(self) -> frozenset:
        """Все ID, доступные агенту через инструменты: записи таблиц (включая ключи FX) и документы."""
        ids = set(self.documents)
        for rows in self.records.values():
            ids.update(rows)
        return frozenset(ids)

    def table(self, name: str) -> list:
        """Строки таблицы в порядке файла."""
        return getattr(self, name)


def _read_csv(path: Path) -> list:
    """Читает CSV (UTF-8) как список словарей строк."""
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _ledger_row(raw: dict) -> dict:
    """Приводит строку книги к типам: суммы -> int, пустые поля -> None."""
    row = {k: (None if v == "" else v) for k, v in raw.items()}
    for k in ("debit_minor", "credit_minor"):
        row[k] = int(row[k]) if row[k] is not None else None
    return row


def load_public(episode_dir) -> Episode:
    """Читает public/ эпизода. private/ не трогает."""
    pub = Path(episode_dir) / "public"
    return Episode(
        manifest=_read_json(pub / "manifest.json"),
        policy=_read_json(pub / "policy.json"),
        ledger=[_ledger_row(r) for r in _read_csv(pub / "ledger.csv")],
        invoices=read_jsonl(pub / "invoices.jsonl"),
        payments=read_jsonl(pub / "payments.jsonl"),
        fx=_read_csv(pub / "fx.csv"),
    )


def _published_tolerances(policy, issue_type) -> list:
    """Допуски, опубликованные в policy для типа: общий и по типу (что есть и корректно)."""
    found = []
    if not isinstance(policy, dict):
        return found
    by_type = policy.get("amount_tolerance_minor_by_type")
    for value in (policy.get("amount_tolerance_minor"),
                  by_type.get(issue_type) if isinstance(by_type, dict) else None):
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            found.append(value)
    return found


def apply_policy_tolerance(truth, policy):
    """τ каждой ошибки = минимум из truth и опубликованного в policy (правила жюри: τ задан
    заранее и публично). Так фикстура с τ=0 в policy не станет мягче из-за truth, и наоборот.
    Структуру не проверяет: битый truth дальше отклонит validate_truth."""
    if not isinstance(truth, dict) or not isinstance(truth.get("issues"), list):
        return truth
    for issue in truth["issues"]:
        if not isinstance(issue, dict):
            continue
        tau = issue.get("amount_tolerance_minor")
        caps = _published_tolerances(policy, issue.get("type"))
        if caps and isinstance(tau, int) and not isinstance(tau, bool):
            issue["amount_tolerance_minor"] = min([tau, *caps])
    return truth


def load_truth(episode_dir) -> dict:
    """Читает private/truth.json и применяет допуск из public/policy.json. Только для сервера."""
    ep = Path(episode_dir)
    truth = _read_json(ep / "private" / "truth.json")
    policy_path = ep / "public" / "policy.json"
    if policy_path.exists():
        try:
            policy = _read_json(policy_path)
        except ValueError:
            policy = None  # битый policy отклонит load_public
        truth = apply_policy_tolerance(truth, policy)
    return truth
