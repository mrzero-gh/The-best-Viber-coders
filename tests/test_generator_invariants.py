"""Генератор, этап 6: инварианты на 60 seed со случайными конфигами, explain, среда, скорость (задача #23).

Конфиги строятся из seed детерминированно: архетип, базовая валюта (KZT или USD), часовой пояс,
набор валют, размеры, включённые ошибки, сценарии и отвлекающие случаи.
"""
import csv
import json
import random
import re
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pytest

from generator.build import LEDGER_FIELDS, generate_episode
from generator.config import DISTRACTORS, ERROR_TYPES, INTERACTIONS
from generator.explain import explain
from tests.test_generator_clean import load

TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
TIMEZONES = ("+05:00", "+06:00", "+03:00", "+00:00", "-04:00", "+09:00", "+05:30")
CURRENCIES = ("USD", "CNY", "EUR", "RUB", "JPY", "GBP", "KZT")
SEEDS = range(1000, 1060)


def random_config(seed: int) -> dict:
    r = random.Random(seed)
    base = "USD" if r.random() < 0.2 else "KZT"
    foreign = [c for c in r.sample(CURRENCIES, r.randint(0, 5)) if c != base]
    rows_lo = r.randint(200, 1500)
    docs_lo = r.randint(20, 150)

    def toggle(lo_max, hi_max):
        lo = r.randint(0, lo_max)
        return {"enabled": r.random() < 0.8, "count": [lo, r.randint(lo, hi_max)]}
    return {"config_version": f"rand-{seed}", "mode": r.choice(["train", "eval"]),
            "company": {"archetype": r.choice(["importer", "saas", "distributor"]), "base_currency": base,
                        "timezone": r.choice(TIMEZONES), "foreign_currencies": foreign},
            "size": {"ledger_rows": [rows_lo, r.randint(rows_lo, 2000)],
                     "documents": [docs_lo, r.randint(docs_lo, 200)]},
            "errors": {t: toggle(2, 3) for t in ERROR_TYPES},
            "interactions": {s: toggle(1, 2) for s in INTERACTIONS},
            "distractors": {k: toggle(1, 3) for k in DISTRACTORS},
            "clean": r.random() < 0.15, "max_errors": r.randint(0, 12)}


@pytest.fixture(scope="module")
def episodes(tmp_path_factory):
    out = {}
    root = tmp_path_factory.mktemp("inv")
    for seed in SEEDS:
        path = generate_episode(seed, random_config(seed), root / str(seed))
        out[seed] = (path, load(path))
    return out


def _all_timestamps(d):
    return ([r["timestamp"] for r in d["ledger"]] + [i["issued_at"] for i in d["invoices"]]
            + [i["due_at"] for i in d["invoices"]] + [p["timestamp"] for p in d["payments"]]
            + [d["manifest"]["period_start"], d["manifest"]["period_end"]])


def test_invariants_on_random_configs(episodes):
    for seed, (path, d) in episodes.items():
        m, cfg = d["manifest"], d["generation"]["config"]
        where = f"seed {seed}"
        # баланс по каждому документу и по всей книге
        groups = defaultdict(int)
        for r in d["ledger"]:
            assert (r["debit_minor"] > 0) != (r["credit_minor"] > 0), where
            assert r["currency"] == m["base_currency"], where
            groups[r["payment_id"] or r["invoice_id"]] += r["debit_minor"] - r["credit_minor"]
        assert all(v == 0 for v in groups.values()), where
        # время: ISO-8601 со смещением, в часовом поясе манифеста
        assert m["timezone"] == cfg["company"]["timezone"], where
        assert all(TS.match(ts) and ts.endswith(m["timezone"]) for ts in _all_timestamps(d)), where
        # размеры эпизода
        assert 200 <= len(d["ledger"]) <= 2000 and 20 <= len(d["invoices"]) + len(d["payments"]) <= 200, where
        assert m["counts"]["ledger_rows"] == len(d["ledger"]), where
        assert len(d["truth"]["issues"]) <= min(12, cfg["max_errors"]), where
        if cfg["clean"]:
            assert d["truth"]["issues"] == [], where
        for t in ERROR_TYPES:  # выключенный тип не появляется
            if not cfg["errors"][t]["enabled"]:
                assert all(i["type"] != t or i["scenario"] for i in d["truth"]["issues"]), where


def test_explicit_nulls_and_csv_shape(episodes):
    for seed, (path, d) in episodes.items():
        with open(path / "public" / "ledger.csv", encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        assert rows[0] == LEDGER_FIELDS, seed
        assert all(len(r) == len(LEDGER_FIELDS) for r in rows), seed
        for p in d["payments"]:
            assert "invoice_refs" in p and isinstance(p["invoice_refs"], list)
        raw = (path / "public" / "ledger.csv").read_bytes()
        assert b"\r" not in raw and not raw.startswith(b"\xef\xbb\xbf"), "LF и без BOM"


def test_certificate_on_random_configs(episodes):
    certificate = pytest.importorskip("grader.certificate")
    for seed, (path, _) in episodes.items():
        cert = certificate.certify_episode(path)
        assert cert["valid"], (seed, [x for x in cert["issues"] + cert["distractors"] if not x.get("valid")][:2])


def test_planned_vs_injected_mostly_met(episodes):
    """Генератор вносит запрошенное, если хватает кандидатов; недобор редок и виден в generation.json."""
    planned = injected = 0
    for _, (_, d) in episodes.items():
        g = d["generation"]
        planned += sum(g["errors_planned"].values()) + 2 * sum(g["interactions_planned"].values())
        injected += len(d["truth"]["issues"])
    assert injected >= 0.9 * planned, (injected, planned)


def test_reproducible_by_hash(tmp_path):
    for seed in (1000, 1007, 1033):
        a = load(generate_episode(seed, random_config(seed), tmp_path / f"a{seed}"))["generation"]
        b = load(generate_episode(seed, random_config(seed), tmp_path / f"b{seed}"))["generation"]
        assert a["episode_sha256"] == b["episode_sha256"]


def test_generation_is_fast(tmp_path):
    cfg = {"size": {"ledger_rows": [2000, 2000], "documents": [200, 200]},
           "errors": {t: {"enabled": True, "count": [3, 3]} for t in ERROR_TYPES}}
    t0 = time.perf_counter()
    generate_episode(5, cfg, tmp_path)
    assert time.perf_counter() - t0 < 10, "эпизод на 2000 проводок генерируется за секунды"


def test_explain_covers_every_issue_and_distractor(episodes):
    for seed in list(SEEDS)[:20]:
        path, d = episodes[seed]
        text = explain(path)
        for i in d["truth"]["issues"]:
            assert i["issue_id"] in text and str(i["affected_ids"][0]) in text
            assert i["issue_id"] in explain(path, i["issue_id"])
        for x in d["truth"]["distractors"]:
            assert x["distractor_id"] in text
    with pytest.raises(KeyError):
        explain(episodes[1000][0], "ISS-NOTEXIST")


def test_server_runs_generated_episode():
    """Сквозной путь: env.server генерирует эпизод этим генератором, оценщик считает награду."""
    client = pytest.importorskip("env.client")
    with client.AuditEnv() as env:
        obs, _ = env.reset(42, {"config_version": "e2e-v1", "mode": "train", "clean": True})
        assert obs["kind"] == "initial" and obs["manifest"]["counts"]["ledger_rows"] >= 200
        o, *_ = env.step({"tool": "list_records", "args": {"table": "ledger", "limit": 50}})
        assert o["kind"] == "result" and len(o["result"]["records"]) == 50
        findings = {"episode_id": obs["manifest"]["episode_id"], "findings": []}
        o, reward, terminated, _, info = env.step({"tool": "submit", "args": {"findings": findings}})
        assert terminated and info["metrics"]["f1"] == 1.0
        assert reward == pytest.approx(1 - 0.002 * 1), "чистая компания: молчание верно, минус шаг"
