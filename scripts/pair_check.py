"""Репетиция блоков 2 и 4 жюри: 20 сценариев D01-D20 и 20 парных проверок P01-P20.

    python -m scripts.pair_check [--only P01,P06] [--json report.json] [--keep DIR]

Для каждой строки матрицы:
1. эпизод сценария D генерируется через judge_adapter.generate_episode;
2. состав truth соответствует сценарию, сертификат оценщика (grader.certificate) подтверждает
   каждую ошибку по открытым данным — и на исходном эпизоде, и на копии;
3. копию готовит scripts.transform_episode (P01-P05 перестановка, P06-P10 смена ID,
   P11-P20 оба), как это делает жюри;
4. oracle проходит оба варианта через judge_adapter с одинаковым числом исследовательских шагов;
   повтор на исходном эпизоде точно воспроизводит переходы; после обратного отображения ID
   findings совпадают; TP/FP/FN/P/R/F1 и награда совпадают.
"""
import argparse
import json
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

import judge_adapter as ja
from generator.config import DISTRACTORS, ERROR_TYPES, INTERACTIONS
from grader.certificate import certify
from grader.evidence import read_public
from scripts.transform_episode import invert, remap_value, transform_episode

_OFF_ERR = {t: {"enabled": False} for t in ERROR_TYPES}
_OFF_DST = {k: {"enabled": False} for k in DISTRACTORS}


def _clean(distractor=None):
    d = dict(_OFF_DST)
    if distractor:
        d[distractor] = {"enabled": True, "count": [1, 1]}
    return {"clean": True, "distractors": d}


def _only(error_type):
    return {"errors": dict(_OFF_ERR, **{error_type: {"enabled": True, "count": [1, 1]}})}


def _inter(scenario):
    return {"errors": _OFF_ERR, "interactions": {scenario: {"enabled": True, "count": [1, 1]}}}


# (код, описание, config генератора, ожидаемые типы ошибок)
SCENARIOS = (
    [("D01", "чистая: обычная сверка", _clean(), Counter())]
    + [("D02", "чистая: разбитый платёж", _clean("split_payment"), Counter()),
       ("D03", "чистая: возврат", _clean("refund"), Counter()),
       ("D04", "чистая: законные повторяющиеся суммы", _clean("legit_recurring"), Counter()),
       ("D05", "чистая: поздний вечер на границе периода", _clean("late_evening_local"), Counter())]
    + [("D%02d" % i, "дубль счёта", _only("duplicate_invoice"), Counter(duplicate_invoice=1))
       for i in (6, 7, 8)]
    + [("D%02d" % i, "распределение платежа", _only("payment_allocation"), Counter(payment_allocation=1))
       for i in (9, 10, 11)]
    + [("D%02d" % i, "дата/курс конвертации", _only("fx_conversion"), Counter(fx_conversion=1))
       for i in (12, 13)]
    + [("D%02d" % i, "ошибка периода", _only("period_cutoff"), Counter(period_cutoff=1))
       for i in (14, 15)]
    + [("D%02d" % i, "взаимодействие: " + INTERACTIONS[0], _inter(INTERACTIONS[0]),
        Counter(payment_allocation=1, period_cutoff=1)) for i in (16, 17, 18)]
    + [("D%02d" % i, "взаимодействие: " + INTERACTIONS[1], _inter(INTERACTIONS[1]),
        Counter(duplicate_invoice=1, fx_conversion=1)) for i in (19, 20)]
)


def transform_kind(n: int) -> tuple:
    """P01-P05: перестановка; P06-P10: смена ID; P11-P20: оба (permute, remap)."""
    if n <= 5:
        return True, False
    if n <= 10:
        return False, True
    return True, True


def _truth(ep):
    return json.loads((Path(ep) / "private" / "truth.json").read_text(encoding="utf-8"))


def _prov(ep):
    return json.loads((Path(ep) / "private" / "provenance.json").read_text(encoding="utf-8"))


def oracle_findings(ep):
    t = _truth(ep)
    return {"episode_id": t["episode_id"], "findings": [
        {"type": i["type"], "affected_ids": sorted(i["affected_ids"]),
         "claimed_amount_minor": i["expected_amount_minor"], "currency": i["currency"],
         "evidence_ids": sorted(i["evidence_sets"][0]), "explanation": "oracle"}
        for i in sorted(t["issues"], key=lambda i: (i["type"], sorted(i["affected_ids"])))]}


def _canon(findings_obj):
    """Сравнение findings без учёта порядка выводов и ID внутри списков."""
    return sorted(json.dumps(dict(f, affected_ids=sorted(f["affected_ids"]),
                                  evidence_ids=sorted(f["evidence_ids"])), sort_keys=True)
                  for f in findings_obj["findings"])


EXPLORE = [{"name": "list_records", "args": {"table": t, "cursor": None, "limit": 50}}
           for t in ("ledger", "invoices", "payments", "fx")]


def run_oracle(env, ep):
    """Одинаковые исследовательские шаги + submit oracle. Возвращает (переходы, findings, шаги)."""
    trans = [env.reset(0, {"mode": "closed", "episode_dir": str(ep), "team_config": {}})]
    for a in EXPLORE:
        trans.append(env.step(a))
    fin = oracle_findings(ep)
    trans.append(env.step({"name": "submit", "args": {"findings": fin}}))
    return trans, fin, len(EXPLORE)


def check_pair(n: int, base: Path, env) -> dict:
    code, title, cfg, expected = SCENARIOS[n - 1]
    pid = "P%02d" % n
    permute, remap = transform_kind(n)
    row = {"pair": pid, "scenario": code, "title": title, "permute": permute, "remap": remap,
           "problems": []}
    bad = row["problems"].append
    src, var = base / code / "src", base / code / "var"
    ja.generate_episode(100 + n, cfg, str(src))

    # блок 2: состав ошибок и независимое подтверждение по открытым данным
    got = Counter(i["type"] for i in _truth(src)["issues"])
    if got != expected:
        bad("состав truth %s, ожидалось %s" % (dict(got), dict(expected)))
    want = [k for k, v in cfg.get("distractors", {}).items() if v.get("enabled")]
    kinds = {d["kind"] for d in _truth(src)["distractors"]}
    if cfg.get("clean") and not set(want) <= kinds:
        bad("нет отвлекающего случая %s" % want)
    if not certify(read_public(src / "public"), _truth(src), _prov(src))["valid"]:
        bad("сертификат не подтвердил исходный эпизод")

    # блок 4: копия как у жюри
    mapping = transform_episode(src, var, seed=n, permute=permute, remap=remap)
    if not certify(read_public(var / "public"), _truth(var), _prov(var))["valid"]:
        bad("сертификат не подтвердил копию")

    t1, fin_src, steps = run_oracle(env, src)
    t2, _, _ = run_oracle(env, src)
    if t1 != t2:
        bad("повтор на исходном эпизоде не совпал")
    tv, fin_var, steps_v = run_oracle(env, var)
    if tv[-1][1:4] != t1[-1][1:4] or tv[-1][0] != t1[-1][0]:
        bad("финальный переход отличается")
    if _canon(remap_value(fin_var, invert(mapping))) != _canon(fin_src):
        bad("findings oracle после обратного отображения ID не совпали")
    s_src = ja.score_episode(str(src), fin_src, steps)
    s_var = ja.score_episode(str(var), fin_var, steps_v)
    if s_src != s_var:
        bad("оценки различаются: %s vs %s" % (s_src, s_var))
    if s_src["f1"] != 1.0:
        bad("oracle не получил F1=1: %s" % s_src)
    row.update(ids_remapped=len(mapping), score=s_src, ok=not row["problems"])
    return row


def _parse_only(text: str, ap) -> list:
    """--only P01,P06 -> [1, 6]; номер вне 1..20 - ошибка (P0 иначе молча стал бы P20)."""
    nums = []
    for part in text.split(","):
        raw = part.strip().lstrip("Pp")
        if not raw.isdigit() or not 1 <= int(raw) <= len(SCENARIOS):
            ap.error("--only: пары P01-P%02d, получено %r" % (len(SCENARIOS), part.strip()))
        nums.append(int(raw))
    return nums


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="scripts.pair_check")
    ap.add_argument("--only", default=None, help="например P01,P06,P11")
    ap.add_argument("--json", default=None, help="сохранить отчёт в JSON")
    ap.add_argument("--keep", default=None, help="папка для эпизодов (по умолчанию временная)")
    a = ap.parse_args(argv)
    nums = range(1, 21) if not a.only else _parse_only(a.only, ap)
    base = Path(a.keep) if a.keep else Path(tempfile.mkdtemp(prefix="pair-check-"))
    rows = []
    try:
        with ja.make_env() as env:
            for n in nums:
                row = check_pair(n, base, env)
                rows.append(row)
                kind = "+".join(k for k, v in (("перестановка", row["permute"]), ("смена ID", row["remap"])) if v)
                print("%s %s %-44s %-24s %s" % (row["pair"], row["scenario"], row["title"], kind,
                                                 "OK" if row["ok"] else "FAIL: " + "; ".join(row["problems"])))
    finally:
        if not a.keep:
            shutil.rmtree(base, ignore_errors=True)
    ok = sum(r["ok"] for r in rows)
    print("Итого: %d/%d пар прошли" % (ok, len(rows)))
    if a.json:
        Path(a.json).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
