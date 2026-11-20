"""Детерминированная сборка мини-эпизода для тестов среды.

Запуск: python -m tests.fixtures.build_mini
Внесена одна ошибка payment_allocation (разнесена только половина платежа).
"""
import json
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

from common.jsonio import write_jsonl

EPISODE_ID = "EP-8K2M4Q7X"
OUT_ROOT = Path(__file__).resolve().parent / "mini"
FX_START = date(2026, 8, 22)
FX_DAYS = 51  # по 2026-10-11 включительно
LEDGER_HEADER = ["row_id", "timestamp", "account", "debit_minor", "credit_minor",
                 "currency", "entity_id", "invoice_id", "payment_id"]


def rate_for(day: str) -> Decimal:
    """Курс USD→KZT на день (строка YYYY-MM-DD)."""
    i = (date.fromisoformat(day) - FX_START).days
    return Decimal("470.0000") + Decimal("0.0500") * i


def to_kzt(usd_minor: int, day: str) -> int:
    """Конвертация minor USD в minor KZT, ROUND_HALF_EVEN до целого."""
    return int((Decimal(usd_minor) * rate_for(day)).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


def _dump(path: Path, obj) -> None:
    """JSON с отступом 2, UTF-8, LF."""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def build(out_root: Path) -> Path:
    """Пишет эпизод в out_root/EP-.../{public,private}; возвращает папку эпизода."""
    ep = Path(out_root) / EPISODE_ID
    pub, priv = ep / "public", ep / "private"
    pub.mkdir(parents=True, exist_ok=True)
    priv.mkdir(parents=True, exist_ok=True)
    e1, e2 = "E-4NV7Q2HC", "E-9XK2M7PB"
    tz = "+05:00"

    # --- fx.csv ---
    fx_rows = []
    for i in range(FX_DAYS):
        d = (FX_START + timedelta(days=i)).isoformat()
        fx_rows.append((d, format(rate_for(d), "f")))
    with open(pub / "fx.csv", "w", encoding="utf-8", newline="\n") as f:
        f.write("date,source_currency,target_currency,rate\n")
        for d, rate in fx_rows:
            f.write(f"{d},USD,KZT,{rate}\n")

    # --- счета и платежи ---
    invoices = [
        {"doc_id": "DOC-7C2K9W4M", "invoice_id": "INV-3HZ7QK2D", "entity_id": e1,
         "issued_at": "2026-09-02T12:00:00+05:00", "due_at": "2026-10-02T12:00:00+05:00",
         "currency": "USD", "total_minor": 250000,
         "line_items": [{"description": "Ноутбуки, партия сентябрь 2026", "quantity": 2,
                         "unit_price_minor": 125000, "amount_minor": 250000}]},
        {"doc_id": "DOC-2PM8K5XA", "invoice_id": "INV-6TQ4W8NR", "entity_id": e2,
         "issued_at": "2026-09-10T09:00:00+05:00", "due_at": "2026-10-10T09:00:00+05:00",
         "currency": "KZT", "total_minor": 50000000,
         "line_items": [{"description": "Консультационные услуги, сентябрь 2026", "quantity": 1,
                         "unit_price_minor": 50000000, "amount_minor": 50000000}]},
        {"doc_id": "DOC-9WN4R7TE", "invoice_id": "INV-8BV2C6MJ", "entity_id": e2,
         "issued_at": "2026-09-20T15:00:00+05:00", "due_at": "2026-10-20T15:00:00+05:00",
         "currency": "KZT", "total_minor": 12000000,
         "line_items": [{"description": "Поддержка, сентябрь 2026", "quantity": 1,
                         "unit_price_minor": 12000000, "amount_minor": 12000000}]},
    ]
    payments = [
        {"doc_id": "DOC-5RW2N8CA", "payment_id": "PAY-9F4TX1MB", "timestamp": "2026-09-25T11:00:00+05:00",
         "currency": "USD", "amount_minor": 250000,
         "invoice_refs": [{"invoice_id": "INV-3HZ7QK2D", "amount_minor": 250000}]},
        {"doc_id": "DOC-6YT3B9VG", "payment_id": "PAY-2KD7H4QS", "timestamp": "2026-09-28T10:00:00+05:00",
         "currency": "KZT", "amount_minor": 50000000,
         "invoice_refs": [{"invoice_id": "INV-6TQ4W8NR", "amount_minor": 50000000}]},
    ]
    write_jsonl(pub / "invoices.jsonl", invoices)
    write_jsonl(pub / "payments.jsonl", payments)

    # --- книга: (ts, account, debit, credit, entity, invoice, payment, закреплённый row_id) ---
    t_i1, t_i2, t_i3 = (invoices[k]["issued_at"] for k in range(3))
    t_p1, t_p2 = payments[0]["timestamp"], payments[1]["timestamp"]
    a_inv = to_kzt(250000, "2026-09-02")
    a_half_inv = to_kzt(125000, "2026-09-02")
    a_pay = to_kzt(250000, "2026-09-25")
    a_half_pay = to_kzt(125000, "2026-09-25")
    a_bal = a_pay - a_half_inv - a_half_pay
    P1, P2 = "PAY-9F4TX1MB", "PAY-2KD7H4QS"
    I1, I2, I3 = "INV-3HZ7QK2D", "INV-6TQ4W8NR", "INV-8BV2C6MJ"
    draft = [
        (t_i1, "1200", a_inv, 0, e1, I1, None, None),
        (t_i1, "4000", 0, a_inv, e1, I1, None, None),
        (t_i2, "1200", 50000000, 0, e2, I2, None, None),
        (t_i2, "4000", 0, 50000000, e2, I2, None, None),
        (t_i3, "1200", 12000000, 0, e2, I3, None, None),
        (t_i3, "4000", 0, 12000000, e2, I3, None, None),
        (t_p1, "1010", a_pay, 0, e1, None, P1, "L-0Q7M2ZK4"),
        (t_p1, "1200", 0, a_half_inv, e1, I1, P1, None),
        (t_p1, "2900", 0, a_half_pay, e1, None, P1, "L-7TBC4X1P"),
        (t_p1, "7900", 0, a_bal, e1, None, P1, None),
        (t_p2, "1010", 50000000, 0, e2, None, P2, None),
        (t_p2, "1200", 0, 50000000, e2, I2, P2, None),
    ]
    free_ids = iter(["L-1A2B3C4D", "L-2E3F4G5H", "L-3J4K5M6N", "L-4P5Q6R7S", "L-5T6V7W8X",
                     "L-6Y7Z8A9B", "L-7C8D9E0F", "L-8G9H0J1K", "L-9M0N1P2Q", "L-0R1S2T3V"])
    ledger = []
    for ts, acc, dr, cr, ent, inv, pay, fixed in draft:
        rid = fixed or next(free_ids)
        ledger.append({"row_id": rid, "timestamp": ts, "account": acc, "debit_minor": dr,
                       "credit_minor": cr, "currency": "KZT", "entity_id": ent,
                       "invoice_id": inv, "payment_id": pay})
    ledger.sort(key=lambda r: (r["timestamp"], r["row_id"]))
    with open(pub / "ledger.csv", "w", encoding="utf-8", newline="\n") as f:
        f.write(",".join(LEDGER_HEADER) + "\n")
        for r in ledger:
            f.write(",".join("" if r[c] is None else str(r[c]) for c in LEDGER_HEADER) + "\n")

    # --- manifest / policy ---
    _dump(pub / "manifest.json", {
        "schema_version": "1.0", "episode_id": EPISODE_ID, "base_currency": "KZT",
        "period_start": "2026-09-01T00:00:00+05:00", "period_end": "2026-10-01T00:00:00+05:00",
        "timezone": tz,
        "counts": {"ledger_rows": 12, "invoices": 3, "payments": 2, "fx_rates": FX_DAYS, "documents": 5},
        "rounding": {"mode": "ROUND_HALF_EVEN"},
        "currency_exponents": {"KZT": 2, "USD": 2}, "csv_null": "", "config_version": "fixture-mini"})
    _dump(pub / "policy.json", {
        "schema_version": "1.0",
        "fx": {"rate_date": "document_local_date", "direction": "1 source_currency = rate target_currency",
               "target_currency": "KZT"},
        "period": {"timezone": tz, "start_inclusive": True, "end_exclusive": True},
        "rounding": {"mode": "ROUND_HALF_EVEN", "apply": "after_conversion_to_target_exponent"},
        "amount_tolerance_minor": 1})

    # --- private ---
    evidence = sorted(["DOC-5RW2N8CA"] + [r["row_id"] for r in ledger if r["payment_id"] == P1])
    _dump(priv / "truth.json", {
        "schema_version": "1.0", "episode_id": EPISODE_ID,
        "issues": [{"issue_id": "ISS-6M3KQ8WD", "type": "payment_allocation",
                    "affected_ids": [I1, P1], "expected_amount_minor": 125000, "currency": "USD",
                    "amount_tolerance_minor": 1, "evidence_sets": [evidence], "scenario": None}],
        "distractors": []})
    _dump(priv / "provenance.json", {
        "episode_id": EPISODE_ID, "generator_version": "fixture-mini", "clean_history_sha256": "",
        "mutations": [{"issue_id": "ISS-6M3KQ8WD", "type": "payment_allocation",
                       "changes": [{"table": "ledger", "record_id": "L-7TBC4X1P", "field": "account",
                                    "before": "1200", "after": "2900"}]}]})
    _dump(priv / "generation.json", {"seed": None, "config": None, "generator_version": "fixture-mini"})
    return ep


if __name__ == "__main__":
    print(build(OUT_ROOT))
