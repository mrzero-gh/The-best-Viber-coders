"""История каждой ошибки и отвлекающего случая — только для оценщика (читает private/).

Запуск:
    python -m generator.explain <папка эпизода>              # все ошибки и отвлекающие случаи
    python -m generator.explain <папка эпизода> ISS-XXXXXXXX # одна ошибка
    python -m generator.explain <папка эпизода> DST-XXXXXXXX # один отвлекающий случай
"""
import argparse
import csv
import json
import sys
from pathlib import Path

from common.jsonio import read_jsonl

TYPE_RU = {"duplicate_invoice": "Дубль счёта", "payment_allocation": "Неполное распределение платежа",
           "fx_conversion": "Неверный курс конвертации", "period_cutoff": "Ошибка отнесения к периоду"}
KIND_RU = {"split_payment": "Разбитый платёж", "refund": "Возврат",
           "legit_recurring": "Законная повторяющаяся сумма", "late_evening_local": "Поздний вечер по местному времени"}
SCENARIO_RU = {"partial_fx_payment_at_cutoff": "частичный валютный платёж на границе периода",
               "duplicate_invoice_wrong_fx": "дубль валютного счёта с неверным курсом"}


class Episode:
    def __init__(self, path: Path):
        pub, priv = path / "public", path / "private"
        rd = lambda p: json.loads(p.read_text(encoding="utf-8"))
        self.manifest = rd(pub / "manifest.json")
        self.truth = rd(priv / "truth.json")
        self.provenance = rd(priv / "provenance.json")
        self.invoices = {i["invoice_id"]: i for i in read_jsonl(pub / "invoices.jsonl")}
        self.payments = {p["payment_id"]: p for p in read_jsonl(pub / "payments.jsonl")}
        with open(pub / "ledger.csv", encoding="utf-8", newline="") as f:
            self.ledger = list(csv.DictReader(f))
        self.mutations = {m["issue_id"]: m for m in self.provenance["mutations"]}

    def money(self, minor: int, currency: str) -> str:
        exp = self.manifest["currency_exponents"].get(currency, 2)
        sign = "−" if minor < 0 else ""
        whole, frac = divmod(abs(minor), 10 ** exp)
        text = f"{whole:,}".replace(",", " ")
        return f"{sign}{text},{frac:0{exp}d} {currency}" if exp else f"{sign}{text} {currency}"

    def doc(self, key: str) -> dict:
        return self.invoices.get(key) or self.payments[key]

    @staticmethod
    def when(doc: dict) -> str:
        return doc.get("issued_at") or doc["timestamp"]

    def amount(self, doc: dict) -> str:
        return self.money(doc.get("total_minor", doc.get("amount_minor")), doc["currency"])

    def rows_of(self, key: str) -> list:
        if key.startswith("PAY-"):
            return [r for r in self.ledger if r["payment_id"] == key]
        return [r for r in self.ledger if r["invoice_id"] == key and not r["payment_id"]]

    def change(self, issue_id: str, field: str):
        return next((c for c in self.mutations[issue_id]["changes"] if c["field"].startswith(field)), None)


# ---------- истории ошибок ----------

def _duplicate(ep, issue):
    copy = ep.change(issue["issue_id"], "__copy_of__")
    dup, orig = ep.invoices[copy["record_id"]], ep.invoices[copy["after"]]
    return [f"Счёт {dup['invoice_id']} — копия счёта {orig['invoice_id']}: тот же контрагент {orig['entity_id']},",
            f"те же {len(orig['line_items'])} строк(и), та же сумма {ep.amount(orig)}.",
            f"Даты: оригинал {ep.when(orig)}, копия {ep.when(dup)}. Копия проведена в книге повторно:",
            "  " + ", ".join(r["row_id"] for r in ep.rows_of(dup["invoice_id"]))]


def _allocation(ep, issue):
    pid = next(x for x in issue["affected_ids"] if x.startswith("PAY-"))
    iid = next(x for x in issue["affected_ids"] if x.startswith("INV-"))
    pay = ep.payments[pid]
    declared = next(r["amount_minor"] for r in pay["invoice_refs"] if r["invoice_id"] == iid)
    allocated = declared - issue["expected_amount_minor"]
    return [f"Платёж {pid} от {ep.when(pay)} на {ep.amount(pay)} по выписке закрывает счёт {iid}",
            f"на {ep.money(declared, pay['currency'])}, а в книге на 1200 разнесено только "
            f"{ep.money(allocated, pay['currency'])}.",
            f"Остаток {ep.money(issue['expected_amount_minor'], pay['currency'])} ушёл на 2900 (невыясненные)."]


def _fx(ep, issue):
    (key,) = issue["affected_ids"]
    doc, base = ep.doc(key), ep.manifest["base_currency"]
    posted = ep.change(issue["issue_id"], "debit_minor")
    rate = ep.change(issue["issue_id"], "rate_used")
    return [f"Документ {key} от {ep.when(doc)} на {ep.amount(doc)}.",
            f"Верный курс на {ep.when(doc)[:10]}: {rate['before']} {base}; в книге использован {rate['after']}.",
            f"Строка {posted['record_id']}: проведено {ep.money(posted['after'], base)} "
            f"вместо {ep.money(posted['before'], base)}.",
            f"Расхождение {ep.money(issue['expected_amount_minor'], base)}."]


def _cutoff(ep, issue):
    (key,) = issue["affected_ids"]
    doc = ep.doc(key)
    m = ep.manifest
    rows = ep.rows_of(key)
    return [f"Документ {key} на {ep.amount(doc)} датирован {ep.when(doc)} — вне периода",
            f"[{m['period_start']}, {m['period_end']}).",
            f"Но проведён {rows[0]['timestamp']}, внутри периода: " + ", ".join(r["row_id"] for r in rows)]


STORIES = {"duplicate_invoice": _duplicate, "payment_allocation": _allocation,
           "fx_conversion": _fx, "period_cutoff": _cutoff}


def explain_issue(ep, issue) -> str:
    title = f"{issue['issue_id']}  {TYPE_RU[issue['type']]}"
    if issue["scenario"]:
        title += f"  (совмещённый сценарий: {SCENARIO_RU[issue['scenario']]})"
    lines = [title, *("  " + x for x in STORIES[issue["type"]](ep, issue))]
    lines.append(f"  Верный вывод: type={issue['type']}, affected_ids={issue['affected_ids']},")
    lines.append(f"    сумма {ep.money(issue['expected_amount_minor'], issue['currency'])} "
                 f"(допуск {issue['amount_tolerance_minor']} minor)")
    for s in issue["evidence_sets"][:3]:
        lines.append(f"    доказательства: {s}")
    if len(issue["evidence_sets"]) > 3:
        lines.append(f"    … и ещё {len(issue['evidence_sets']) - 3} допустимых наборов")
    return "\n".join(lines)


# ---------- отвлекающие случаи ----------

def explain_distractor(ep, item) -> str:
    ids = item["record_ids"]
    kind = item["kind"]
    if kind == "split_payment":
        inv = ep.invoices[next(x for x in ids if x.startswith("INV-"))]
        pays = [ep.payments[x] for x in ids if x.startswith("PAY-")]
        why = (f"Счёт {inv['invoice_id']} на {ep.amount(inv)} закрыт платежами "
               + " + ".join(f"{p['payment_id']} ({ep.amount(p)})" for p in pays)
               + ": каждый разнесён полностью, в сумме ровно счёт. Ошибки нет.")
    elif kind == "refund":
        pays = sorted((ep.payments[x] for x in ids if x.startswith("PAY-")), key=lambda p: p["amount_minor"])
        back, orig = pays
        why = (f"Возврат {back['payment_id']} ({ep.amount(back)}) после оплаты {orig['payment_id']} "
               f"({ep.amount(orig)}): обратные проводки по тем же правилам курса. Ошибки нет.")
    elif kind == "legit_recurring":
        a, b = sorted((ep.invoices[x] for x in ids), key=ep.when)
        why = (f"Счета {a['invoice_id']} ({ep.when(a)[:10]}) и {b['invoice_id']} ({ep.when(b)[:10]}): "
               f"тот же контрагент и сумма {ep.amount(a)}, но разные месяцы услуги "
               f"(«{a['line_items'][0]['description']}» и «{b['line_items'][0]['description']}»). Не дубль.")
    else:
        doc = ep.doc(ids[0])
        why = (f"Документ {ids[0]} от {ep.when(doc)} — последний вечер периода. Период кончается "
               f"{ep.manifest['period_end']} по местному времени ({ep.manifest['timezone']}), "
               f"документ до этой границы и проведён в периоде. Ошибки нет.")
    return f"{item['distractor_id']}  {KIND_RU[kind]}\n  {why}"


def explain(episode_dir, item_id=None) -> str:
    ep = Episode(Path(episode_dir))
    issues = {i["issue_id"]: i for i in ep.truth["issues"]}
    dists = {d["distractor_id"]: d for d in ep.truth["distractors"]}
    if item_id:
        if item_id in issues:
            return explain_issue(ep, issues[item_id])
        if item_id in dists:
            return explain_distractor(ep, dists[item_id])
        raise KeyError(item_id)
    m = ep.manifest
    head = [f"Эпизод {m['episode_id']}: период [{m['period_start']}, {m['period_end']}), "
            f"база {m['base_currency']}, ошибок {len(issues)}, отвлекающих случаев {len(dists)}"]
    body = [explain_issue(ep, i) for i in issues.values()] + [explain_distractor(ep, d) for d in dists.values()]
    return "\n\n".join(head + body)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Причинная история ошибок эпизода (только для оценщика)")
    p.add_argument("episode")
    p.add_argument("item_id", nargs="?", help="ISS-… или DST-…")
    a = p.parse_args(argv)
    try:
        text = explain(a.episode, a.item_id)
    except KeyError:
        print(f"нет такого ID: {a.item_id}", file=sys.stderr)
        return 1
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
