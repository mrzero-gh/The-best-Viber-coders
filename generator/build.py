"""Построение эпизода на диске: public/ (6 файлов) и private/ (truth, provenance, generation).

Запуск: python -m generator.build --seed 42 --out episodes/train [--config путь.json]
"""
import argparse
import csv
import hashlib
import json
from datetime import datetime
from pathlib import Path

from common.jsonio import canonical_dumps, write_jsonl
from generator.config import normalize_config
from generator.history import build_clean
from generator.rng import IdFactory, stream

GENERATOR_VERSION = "0.1.0"
SCHEMA_VERSION = "1.0"
LEDGER_FIELDS = ["row_id", "timestamp", "account", "debit_minor", "credit_minor",
                 "currency", "entity_id", "invoice_id", "payment_id"]
FX_FIELDS = ["date", "source_currency", "target_currency", "rate"]
PUBLIC_FILES = ("manifest.json", "ledger.csv", "invoices.jsonl", "payments.jsonl", "fx.csv", "policy.json")


def _instant(ts: str):
    return datetime.fromisoformat(ts)


def _write_json(path: Path, obj) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(canonical_dumps(obj) + "\n")


def _write_csv(path: Path, fields, rows) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(fields)
        for r in rows:
            w.writerow(["" if r[k] is None else r[k] for k in fields])


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sorted_tables(h):
    """Порядок строк в файлах: (время, ID). Логика среды от него не зависит."""
    ledger = sorted(h.ledger, key=lambda r: (_instant(r["timestamp"]), r["row_id"]))
    invoices = sorted(h.invoices, key=lambda r: (_instant(r["issued_at"]), r["invoice_id"]))
    payments = sorted(h.payments, key=lambda r: (_instant(r["timestamp"]), r["payment_id"]))
    fx = sorted(h.fx_rows, key=lambda r: (r["date"], r["source_currency"], r["target_currency"]))
    return ledger, invoices, payments, fx


def make_manifest(h, cfg, counts) -> dict:
    c = h.ctx
    return {"schema_version": SCHEMA_VERSION, "episode_id": h.episode_id, "base_currency": c.base,
            "period_start": c.period_start.isoformat(timespec="seconds"),
            "period_end": c.period_end.isoformat(timespec="seconds"),
            "timezone": c.tz_name, "counts": counts, "rounding": {"mode": "ROUND_HALF_EVEN"},
            "currency_exponents": dict(sorted(c.exps.items())), "csv_null": "",
            "config_version": cfg["config_version"]}


def make_policy(h, tolerance=1) -> dict:
    return {"schema_version": SCHEMA_VERSION,
            "fx": {"rate_date": "document_local_date",
                   "direction": "1 source_currency = rate target_currency",
                   "target_currency": h.ctx.base},
            "period": {"timezone": h.ctx.tz_name, "start_inclusive": True, "end_exclusive": True},
            "rounding": {"mode": "ROUND_HALF_EVEN", "apply": "after_conversion_to_target_exponent"},
            "amount_tolerance_minor": tolerance}


def history_digest(h) -> str:
    """sha256 открытого содержания истории (для provenance.clean_history_sha256)."""
    ledger, invoices, payments, fx = sorted_tables(h)
    blob = canonical_dumps({"ledger": ledger, "invoices": invoices, "payments": payments, "fx": fx})
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def write_episode(h, cfg, out_dir, seed, id_seed, issues=(), distractors=(), mutations=(),
                  clean_sha256=None) -> Path:
    ep = Path(out_dir) / h.episode_id
    pub, priv = ep / "public", ep / "private"
    pub.mkdir(parents=True, exist_ok=True)
    priv.mkdir(parents=True, exist_ok=True)

    ledger, invoices, payments, fx = sorted_tables(h)
    counts = {"ledger_rows": len(ledger), "invoices": len(invoices), "payments": len(payments),
              "fx_rates": len(fx), "documents": len(invoices) + len(payments)}
    _write_json(pub / "manifest.json", make_manifest(h, cfg, counts))
    _write_csv(pub / "ledger.csv", LEDGER_FIELDS, ledger)
    write_jsonl(pub / "invoices.jsonl", invoices)
    write_jsonl(pub / "payments.jsonl", payments)
    _write_csv(pub / "fx.csv", FX_FIELDS, fx)
    _write_json(pub / "policy.json", make_policy(h))

    _write_json(priv / "truth.json", {"schema_version": SCHEMA_VERSION, "episode_id": h.episode_id,
                                      "issues": list(issues), "distractors": list(distractors)})
    _write_json(priv / "provenance.json", {
        "episode_id": h.episode_id, "generator_version": GENERATOR_VERSION,
        "clean_history_sha256": clean_sha256 or history_digest(h), "mutations": list(mutations)})
    hashes = {name: _sha256(pub / name) for name in PUBLIC_FILES}
    fingerprint = hashlib.sha256(canonical_dumps(hashes).encode("utf-8")).hexdigest()
    _write_json(priv / "generation.json", {
        "seed": seed, "id_seed": id_seed, "config": cfg, "generator_version": GENERATOR_VERSION,
        "public_sha256": hashes, "episode_sha256": fingerprint})
    return ep


def generate_episode(seed: int, config: dict, out_dir: "Path", id_seed=None) -> "Path":
    """Генерирует эпизод в out_dir/<episode_id>/public и /private, возвращает путь эпизода.

    id_seed (по умолчанию = seed) задаёт только поток ids: меняет ID, но не содержание.
    """
    cfg = normalize_config(config)
    id_seed = seed if id_seed is None else id_seed
    content = stream(seed, "content")
    ids = IdFactory(stream(id_seed, "ids"))
    h = build_clean(cfg, content, ids)
    return write_episode(h, cfg, out_dir, seed, id_seed)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Генерация эпизода Audit Gym")
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--config", help="JSON-файл конфига (по умолчанию — встроенный)")
    p.add_argument("--id-seed", type=int, default=None)
    a = p.parse_args(argv)
    cfg = json.loads(Path(a.config).read_text(encoding="utf-8")) if a.config else {}
    ep = generate_episode(a.seed, cfg, Path(a.out), id_seed=a.id_seed)
    gen = json.loads((ep / "private" / "generation.json").read_text(encoding="utf-8"))
    print(ep.name, gen["episode_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
