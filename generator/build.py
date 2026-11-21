"""Построение эпизода на диске: public/ (6 файлов) и private/ (truth, provenance, generation).

Запуск: python -m generator.build --seed 42 --out episodes/train [--config путь.json]
"""
import argparse
import copy
import csv
import hashlib
import json
import numbers
from datetime import datetime
from pathlib import Path

from common.jsonio import canonical_dumps, write_jsonl
from generator.config import DOCUMENT_BOUNDS, LEDGER_BOUNDS, normalize_config
from generator.distractors import cap_distractors, make_distractors, plan_distractors
from generator.errors import inject, plan_errors, plan_interactions
from generator.history import build_clean
from generator.rng import IdFactory, stream

SIZE_ATTEMPTS = 4  # сборок под узкий диапазон размеров; обычно хватает первой
GENERATOR_VERSION = "0.6.0"  # 0.6.0: допуск суммы по типам, variant у payment_allocation
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


def make_policy(h) -> dict:
    from generator.errors import TOLERANCE_BY_TYPE
    return {"schema_version": SCHEMA_VERSION,
            "fx": {"rate_date": "document_local_date",
                   "direction": "1 source_currency = rate target_currency",
                   "target_currency": h.ctx.base},
            "period": {"timezone": h.ctx.tz_name, "start_inclusive": True, "end_exclusive": True},
            "rounding": {"mode": "ROUND_HALF_EVEN", "apply": "after_conversion_to_target_exponent"},
            # общий верхний предел и точный допуск каждого типа; оценщик берёт минимум из
            # опубликованного здесь и записанного в truth (env.episode.load_truth)
            "amount_tolerance_minor": max(TOLERANCE_BY_TYPE.values()),
            "amount_tolerance_minor_by_type": dict(TOLERANCE_BY_TYPE)}


def tables_digest(tables) -> str:
    """sha256 четырёх таблиц (ledger, invoices, payments, fx) в порядке sorted_tables."""
    ledger, invoices, payments, fx = tables
    blob = canonical_dumps({"ledger": ledger, "invoices": invoices, "payments": payments, "fx": fx})
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def history_digest(h) -> str:
    """sha256 открытого содержания истории (для provenance.clean_history_sha256)."""
    return tables_digest(sorted_tables(h))


TABLE_NAMES = ("ledger", "invoices", "payments", "fx")


def record_key(table: str, row: dict) -> str:
    """ID записи таблицы; у курса ID - ключ FX:<дата>:<валюта>:<база>, как в доказательствах."""
    if table == "fx":
        return f"FX:{row['date']}:{row['source_currency']}:{row['target_currency']}"
    return row[{"ledger": "row_id", "invoices": "invoice_id", "payments": "payment_id"}[table]]


def history_diff(clean_tables, final_tables) -> dict:
    """Полный обратимый журнал: чем итоговые (открытые) таблицы отличаются от чистой истории.

    removed - строки чистой истории целиком (их нет в открытых данных), added - ID новых строк,
    modified - {id, field, before, after}. Чистая история = открытые данные - added + removed,
    с before вместо after; её sha256 равен provenance.clean_history_sha256."""
    diff = {}
    for name, clean, final in zip(TABLE_NAMES, clean_tables, final_tables):
        cm = {record_key(name, r): r for r in clean}
        fm = {record_key(name, r): r for r in final}
        modified = []
        for key in sorted(set(cm) & set(fm)):
            for field in sorted(set(cm[key]) | set(fm[key])):
                if cm[key].get(field) != fm[key].get(field):
                    modified.append({"id": key, "field": field,
                                     "before": cm[key].get(field), "after": fm[key].get(field)})
        diff[name] = {"removed": [cm[k] for k in sorted(set(cm) - set(fm))],
                      "added": sorted(set(fm) - set(cm)), "modified": modified}
    return diff


def write_episode(h, cfg, out_dir, seed, id_seed, issues=(), distractors=(), mutations=(),
                  clean_sha256=None, extra=None, clean_diff=None) -> Path:
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
        "clean_history_sha256": clean_sha256 or history_digest(h), "mutations": list(mutations),
        "clean_history_diff": clean_diff if clean_diff is not None else history_diff(*[sorted_tables(h)] * 2)})
    hashes = {name: _sha256(pub / name) for name in PUBLIC_FILES}
    fingerprint = hashlib.sha256(canonical_dumps(hashes).encode("utf-8")).hexdigest()
    _write_json(priv / "generation.json", {
        "seed": seed, "id_seed": id_seed, "config": cfg, "generator_version": GENERATOR_VERSION,
        "public_sha256": hashes, "episode_sha256": fingerprint, **(extra or {})})
    return ep


def _int_seed(value, name: str) -> int:
    """seed хэшируется как строка (generator.rng.stream): 5 и 5.0 дали бы разные эпизоды.
    Поэтому принимаем только целое; целое число во float (5.0) приводим к int."""
    if isinstance(value, bool):
        raise ValueError(f"{name}: нужно целое число, а не bool")
    if isinstance(value, numbers.Integral):
        return int(value)  # и numpy.int64 -> обычный int
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise ValueError(f"{name}: нужно целое число")


def generate_episode(seed: int, config: dict, out_dir: "Path", id_seed=None) -> "Path":
    """Генерирует эпизод в out_dir/<episode_id>/public и /private, возвращает путь эпизода.

    id_seed (по умолчанию = seed) задаёт только поток ids: меняет ID, но не содержание.
    """
    cfg = normalize_config(config)
    seed = _int_seed(seed, "seed")
    id_seed = seed if id_seed is None else _int_seed(id_seed, "id_seed")
    best = None
    shift = (0, 0)
    for attempt in range(SIZE_ATTEMPTS):
        built = _build_once(cfg, seed, id_seed, shift)
        miss = _size_miss(cfg, built["h"])
        # выбор: никогда не за пределами кейса; лучше недобор, чем выход за верхнюю границу config
        # (так было и до повторной сборки); затем меньший промах; при равенстве - более ранняя попытка
        rank = (_outside_case(built["h"]), miss[0] < 0 or miss[1] < 0, abs(miss[0]) + abs(miss[1]))
        if best is None or rank < best[0]:
            best = (rank, attempt, shift, built)
        if miss == (0, 0):
            break
        shift = (shift[0] + _aim(miss[0], cfg["size"]["ledger_rows"]),
                 shift[1] + _aim(miss[1], cfg["size"]["documents"]))
    _, attempt, shift, built = best
    extra = dict(built["extra"])
    if attempt:  # в эпизодах, собранных с первой попытки, generation.json не меняется
        extra["size_retry"] = {"attempt": attempt, "shift_rows": shift[0], "shift_documents": shift[1]}
    return write_episode(built["h"], cfg, out_dir, seed, id_seed, built["issues"], built["distractors"],
                         built["mutations"], built["clean_sha"], clean_diff=built["clean_diff"], extra=extra)


def _size_miss(cfg, h) -> tuple:
    """Насколько итог вышел за диапазон config: (строки, документы); >0 - не хватает, <0 - лишние."""
    def miss(value, bounds):
        lo, hi = bounds
        return lo - value if value < lo else hi - value if value > hi else 0
    return (miss(len(h.ledger), cfg["size"]["ledger_rows"]),
            miss(len(h.invoices) + len(h.payments), cfg["size"]["documents"]))


def _outside_case(h) -> bool:
    """Итог вне общих пределов кейса (200-2000 проводок, 20-200 документов)."""
    docs = len(h.invoices) + len(h.payments)
    return not (LEDGER_BOUNDS[0] <= len(h.ledger) <= LEDGER_BOUNDS[1]
                and DOCUMENT_BOUNDS[0] <= docs <= DOCUMENT_BOUNDS[1])


def _aim(miss: int, bounds) -> int:
    """Сдвиг цели: на величину промаха и ещё на полдиапазона внутрь, чтобы не попасть снова на край."""
    if miss == 0:
        return 0
    half = (bounds[1] - bounds[0]) // 2
    return miss + (half if miss > 0 else -half)


def _build_once(cfg, seed: int, id_seed: int, shift) -> dict:
    """Одна сборка в памяти. Потоки rng создаются заново от seed: попытка детерминирована."""
    content = stream(seed, "content")
    errors = stream(seed, "errors")
    dist_rng = stream(seed, "distractors")
    ids = IdFactory(stream(id_seed, "ids"))
    planned = plan_errors(cfg, errors)
    iplanned = plan_interactions(cfg, errors, planned)
    dplanned = plan_distractors(cfg, dist_rng)
    n_errors = sum(planned.values()) + 2 * sum(iplanned.values())
    if n_errors:  # в чистом эпизоде (D02-D05) отвлекающие случаи не урезаются
        dplanned = cap_distractors(dplanned, cfg["size"]["documents"][0], n_errors)
    n_dfx = iplanned["duplicate_invoice_wrong_fx"]
    n_dup = planned["duplicate_invoice"] + n_dfx
    n_one_row = (planned["payment_allocation"] + planned["fx_conversion"]
                 + iplanned["partial_fx_payment_at_cutoff"])
    # разбитый платёж, возврат и прошлый повторяющийся счёт: +1 документ и до 3 строк каждый
    n_dst = dplanned["split_payment"] + dplanned["refund"] + dplanned["legit_recurring"]
    # дубль добавляет документ и до 3 строк; частичное распределение и FX платежа — до 1 строки
    h = build_clean(cfg, content, ids,
                    reserve_rows=3 * (n_dup + n_dst) + n_one_row,
                    reserve_docs=n_dup + n_dst, small_count=2 * n_dup + dplanned["legit_recurring"],
                    base_small=dplanned["legit_recurring"], foreign_small=2 * n_dfx,
                    # +1 оплата в запас: валютный платёж может занять отвлекающий случай
                    foreign_paid=iplanned["partial_fx_payment_at_cutoff"] + 1
                    if iplanned["partial_fx_payment_at_cutoff"] else 0,
                    # fx_conversion нужен валютный документ (у saas или с одной дешёвой валютой их может не быть);
                    # валютные счета занимают ещё сценарий 1 (+1 в запас) и дубль с неверным курсом
                    foreign_docs=2 * planned["fx_conversion"] + 2 * n_dfx
                    + (iplanned["partial_fx_payment_at_cutoff"] + 1 if iplanned["partial_fx_payment_at_cutoff"] else 0),
                    # payment_allocation нужен платёж, сумму которого восстановят по проводке (+1 в запас)
                    recoverable_paid=planned["payment_allocation"] + 1 if planned["payment_allocation"] else 0,
                    size_shift=shift)
    dm = make_distractors(h, dist_rng, ids, dplanned,
                          keep_foreign=planned["fx_conversion"] + 2 * iplanned["partial_fx_payment_at_cutoff"])
    clean_tables = copy.deepcopy(sorted_tables(h))  # чистая история уже с отвлекающими случаями
    clean_sha = tables_digest(clean_tables)
    inj = inject(h, cfg, errors, ids, planned, used=dm.used, interactions=iplanned)
    clean_diff = history_diff(clean_tables, sorted_tables(h))
    injected = {t: sum(1 for i in inj.issues if i["type"] == t) for t in planned}
    dinjected = {k: sum(1 for x in dm.items if x["kind"] == k) for k in dplanned}
    # сценарий засчитан, если обе его ошибки внесены (у каждой scenario = имя)
    iinjected = {s: sum(1 for i in inj.issues if i["scenario"] == s) // 2 for s in iplanned}
    return dict(h=h, issues=inj.issues, distractors=dm.items, mutations=inj.mutations, clean_sha=clean_sha,
                clean_diff=clean_diff,
                extra={"errors_planned": planned, "errors_injected": injected,
                       "interactions_planned": iplanned, "interactions_injected": iinjected,
                       "distractors_planned": dplanned, "distractors_injected": dinjected})


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
