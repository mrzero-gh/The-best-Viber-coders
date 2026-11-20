"""Генератор, этап 1: чистая история (задача #3). Все конфиги здесь с clean: true."""
import csv
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

import pytest

from common.jsonio import read_jsonl
from generator.build import PUBLIC_FILES, generate_episode
from generator.config import ConfigError, normalize_config

SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"
TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
SMALL = {"config_version": "t-small", "mode": "train", "clean": True,
         "size": {"ledger_rows": [200, 260], "documents": [20, 30]}}
BIG = {"config_version": "t-big", "mode": "eval", "clean": True,
       "size": {"ledger_rows": [1800, 2000], "documents": [180, 200]}}
FEW_DOCS_MANY_ROWS = {"config_version": "t-skew", "mode": "train", "clean": True,
                      "size": {"ledger_rows": [1900, 2000], "documents": [20, 22]}}
SAAS_KZT_ONLY = {"config_version": "t-saas", "mode": "train", "clean": True,
                 "company": {"archetype": "saas", "foreign_currencies": []}}
CONFIGS = [{"clean": True}, SMALL, BIG, FEW_DOCS_MANY_ROWS, SAAS_KZT_ONLY]


def load(ep: Path) -> dict:
    pub = ep / "public"
    with open(pub / "ledger.csv", encoding="utf-8", newline="") as f:
        ledger = [{k: (None if v == "" else v) for k, v in r.items()} for r in csv.DictReader(f)]
    for r in ledger:
        r["debit_minor"], r["credit_minor"] = int(r["debit_minor"]), int(r["credit_minor"])
    with open(pub / "fx.csv", encoding="utf-8", newline="") as f:
        fx = list(csv.DictReader(f))
    rd = lambda n: json.loads((ep / n).read_text(encoding="utf-8"))
    return {"manifest": rd("public/manifest.json"), "policy": rd("public/policy.json"),
            "ledger": ledger, "fx": fx,
            "invoices": read_jsonl(pub / "invoices.jsonl"), "payments": read_jsonl(pub / "payments.jsonl"),
            "truth": rd("private/truth.json"), "generation": rd("private/generation.json")}


@pytest.fixture(scope="module", params=list(enumerate(CONFIGS)), ids=lambda p: f"cfg{p[0]}")
def episode(request, tmp_path_factory):
    i, cfg = request.param
    ep = generate_episode(100 + i, cfg, tmp_path_factory.mktemp("ep"))
    return load(ep), normalize_config(cfg)


# ---------- схемы ----------

def _validator(ref):
    jsonschema = pytest.importorskip("jsonschema")
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012
    reg = Registry()
    for p in SCHEMAS.glob("*.schema.json"):
        s = json.loads(p.read_text(encoding="utf-8"))
        reg = reg.with_resource(s["$id"], Resource.from_contents(s, default_specification=DRAFT202012))
    return jsonschema.Draft202012Validator({"$ref": ref}, registry=reg)


def test_schemas(episode):
    d, _ = episode
    base = "https://audit-gym.local/schemas/"
    checks = [("manifest.schema.json", [d["manifest"]]), ("truth.schema.json", [d["truth"]]),
              ("records.schema.json#/$defs/policy", [d["policy"]]),
              ("records.schema.json#/$defs/ledger_row", d["ledger"]),
              ("records.schema.json#/$defs/invoice", d["invoices"]),
              ("records.schema.json#/$defs/payment", d["payments"]),
              ("records.schema.json#/$defs/fx_rate", d["fx"])]
    for ref, rows in checks:
        v = _validator(base + ref)
        for row in rows:
            errors = list(v.iter_errors(row))
            assert not errors, f"{ref}: {errors[0].message}"


# ---------- учёт ----------

def test_balance_whole_and_per_document(episode):
    d, _ = episode
    led = d["ledger"]
    assert sum(r["debit_minor"] for r in led) == sum(r["credit_minor"] for r in led)
    groups = defaultdict(int)
    for r in led:
        key = r["payment_id"] or r["invoice_id"]
        assert key, "у каждой строки есть документ"
        groups[key] += r["debit_minor"] - r["credit_minor"]
    assert all(v == 0 for v in groups.values()), "по каждому документу дебет = кредит"
    for r in led:
        assert (r["debit_minor"] > 0) != (r["credit_minor"] > 0)
        assert r["currency"] == d["manifest"]["base_currency"]


def _convert(amount, cur, day, d):
    m = d["manifest"]
    if cur == m["base_currency"]:
        return amount
    rate = next(Decimal(x["rate"]) for x in d["fx"] if x["date"] == day and x["source_currency"] == cur)
    exps = m["currency_exponents"]
    v = Decimal(amount) * rate * Decimal(10) ** (exps[m["base_currency"]] - exps[cur])
    return int(v.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def test_invoice_postings_match_published_fx(episode):
    """Независимый пересчёт по открытым fx.csv и policy: Дт 1200 = total × курс даты счёта."""
    d, _ = episode
    ar = {r["invoice_id"]: r["debit_minor"] for r in d["ledger"]
          if r["account"] == "1200" and r["debit_minor"] > 0}
    for inv in d["invoices"]:
        assert sum(x["amount_minor"] for x in inv["line_items"]) == inv["total_minor"]
        day = inv["issued_at"][:10]  # время уже в часовом поясе манифеста
        assert ar[inv["invoice_id"]] == _convert(inv["total_minor"], inv["currency"], day, d)


def test_paid_invoices_close_receivable(episode):
    d, _ = episode
    bal = defaultdict(int)
    for r in d["ledger"]:
        if r["account"] == "1200":
            bal[r["invoice_id"]] += r["debit_minor"] - r["credit_minor"]
    paid = {ref["invoice_id"] for p in d["payments"] for ref in p["invoice_refs"]}
    for iid in paid:
        assert bal[iid] == 0
    assert not any(r["account"] == "2900" for r in d["ledger"]), "в чистой истории нет невыясненных"


def test_sizes_period_and_formats(episode):
    d, cfg = episode
    m = d["manifest"]
    assert 200 <= len(d["ledger"]) <= 2000
    lo, hi = cfg["size"]["ledger_rows"]
    assert lo <= len(d["ledger"]) <= hi
    docs = len(d["invoices"]) + len(d["payments"])
    assert 20 <= docs <= 200
    assert m["counts"]["ledger_rows"] == len(d["ledger"]) and m["counts"]["documents"] == docs
    start, end = datetime.fromisoformat(m["period_start"]), datetime.fromisoformat(m["period_end"])
    for ts in ([r["timestamp"] for r in d["ledger"]] + [i["issued_at"] for i in d["invoices"]]
               + [p["timestamp"] for p in d["payments"]]):
        assert TS.match(ts) and start <= datetime.fromisoformat(ts) < end
    assert TS.match(m["period_start"]) and m["timezone"] == "+05:00"


def test_fx_covers_every_day_with_margin(episode):
    d, cfg = episode
    m = d["manifest"]
    first = datetime.fromisoformat(m["period_start"]).date() - timedelta(days=10)
    last = datetime.fromisoformat(m["period_end"]).date() + timedelta(days=10)
    have = {(x["date"], x["source_currency"]) for x in d["fx"]}
    day = first
    while day <= last:
        for cur in cfg["company"]["foreign_currencies"]:
            assert (day.isoformat(), cur) in have
        day += timedelta(days=1)
    assert len(have) == len(d["fx"]), "одна строка на (дата, валюта)"


def test_no_floats_anywhere(episode):
    d, _ = episode

    def walk(x):
        assert not isinstance(x, float)
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    for key in ("manifest", "policy", "invoices", "payments", "truth"):
        walk(d[key])


# ---------- детерминизм ----------

def test_same_seed_same_hashes(tmp_path):
    a = load(generate_episode(7, SMALL, tmp_path / "a"))["generation"]
    b = load(generate_episode(7, SMALL, tmp_path / "b"))["generation"]
    assert a["public_sha256"] == b["public_sha256"]
    c = load(generate_episode(8, SMALL, tmp_path / "c"))["generation"]
    assert a["episode_sha256"] != c["episode_sha256"]
    assert set(a["public_sha256"]) == set(PUBLIC_FILES)


def _content(d):
    """Содержание без ID: суммы, время, счета, валюты."""
    led = sorted((r["timestamp"], r["account"], r["debit_minor"], r["credit_minor"]) for r in d["ledger"])
    inv = sorted((i["issued_at"], i["currency"], i["total_minor"]) for i in d["invoices"])
    pay = sorted((p["timestamp"], p["currency"], p["amount_minor"]) for p in d["payments"])
    return led, inv, pay, d["fx"]


def test_id_stream_changes_ids_not_content(tmp_path):
    a = load(generate_episode(11, {"clean": True}, tmp_path / "a"))
    b = load(generate_episode(11, {"clean": True}, tmp_path / "b", id_seed=999))
    assert _content(a) == _content(b)
    assert {r["row_id"] for r in a["ledger"]}.isdisjoint({r["row_id"] for r in b["ledger"]})


# ---------- конфиг ----------

@pytest.mark.parametrize("bad", [
    {"oops": 1},
    {"mode": "test"},
    {"size": {"ledger_rows": [100, 300]}},
    {"size": {"documents": [50, 40]}},
    {"errors": {"duplicate_invoice": {"enabled": "yes"}}},
    {"company": {"timezone": "UTC"}},
    {"max_errors": 13},
])
def test_bad_config_rejected(bad):
    with pytest.raises(ConfigError):
        normalize_config(bad)
