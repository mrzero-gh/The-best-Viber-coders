"""Внесение ошибок в чистую историю (контракт, раздел 9).

Каждая ошибка — отдельный issue в truth.json и отдельная мутация в provenance.json.
Записи, задействованные одной ошибкой, другие ошибки не трогают: так пара
(type, affected_ids) уникальна, а доказательства не пересекаются.
"""
from datetime import timedelta

from generator.config import ERROR_TYPES
from generator.history import fmt_ts, post_invoice, post_payment
from generator.rng import IdFactory

TOLERANCE_MINOR = 1


def plan_errors(cfg, rng) -> dict:
    """Сколько ошибок каждого типа внести: count из конфига, общий предел max_errors."""
    counts = {t: 0 for t in ERROR_TYPES}
    if cfg["clean"]:
        return counts
    for t in ERROR_TYPES:
        toggle = cfg["errors"][t]
        if toggle["enabled"]:
            counts[t] = rng.randint(*toggle["count"])
    while sum(counts.values()) > cfg["max_errors"]:
        biggest = max(ERROR_TYPES, key=lambda k: counts[k])  # при равенстве — первый по порядку
        counts[biggest] -= 1
    return counts


def _created(table, rows, after):
    return [{"table": table, "record_id": r["row_id"], "field": "__created__", "before": None,
             "after": after} for r in rows]


def _deleted(rows, before):
    return [{"table": "ledger", "record_id": r["row_id"], "field": "__deleted__", "before": before,
             "after": None} for r in rows]


class Injector:
    def __init__(self, history, rng, ids):
        self.h, self.rng, self.ids = history, rng, ids
        self.issue_ids = IdFactory(rng)  # ISS-… случайны и служат канарейками
        self.used = set()                # invoice_id / payment_id, уже задействованные ошибками
        self.issues, self.mutations = [], []

    def _add(self, kind, affected, amount, currency, evidence_sets, changes, scenario=None):
        iid = self.issue_ids.new("ISS")
        self.issues.append({
            "issue_id": iid, "type": kind, "affected_ids": sorted(affected),
            "expected_amount_minor": amount, "currency": currency,
            "amount_tolerance_minor": TOLERANCE_MINOR,
            "evidence_sets": [sorted(s) for s in evidence_sets], "scenario": scenario})
        self.mutations.append({"issue_id": iid, "type": kind, "changes": changes})
        return iid

    def _paid_invoice_ids(self):
        return {ref["invoice_id"] for p in self.h.payments for ref in p["invoice_refs"]}

    # ---------- duplicate_invoice ----------

    def duplicate_invoice(self, scenario=None):
        """Копия счёта (новые invoice_id/doc_id, те же контрагент и строки), дата +0–5 дней,
        проведена в книге повторно. Возвращает (issue_id, дубль) или None, если нет кандидатов."""
        h, ctx = self.h, self.h.ctx
        end_day = ctx.period_end.date()
        small = set(h.small_invoice_ids)
        cands = [inv for inv in h.invoices
                 if inv["invoice_id"] in small and inv["invoice_id"] not in self.used]
        if not cands:
            return None
        src = self.rng.choice(cands)
        src_day = ctx.local_date(src["issued_at"])
        delta = self.rng.randint(0, max(0, min(5, (end_day - src_day).days - 1)))
        day = src_day + timedelta(days=delta)
        issued = ctx.period_start.replace(year=day.year, month=day.month, day=day.day,
                                          hour=self.rng.randint(9, 18), minute=self.rng.randint(0, 59))
        dup = dict(src, doc_id=self.ids.new("DOC"), invoice_id=self.ids.new("INV"),
                   issued_at=fmt_ts(issued), due_at=fmt_ts(issued + timedelta(days=30)),
                   line_items=[dict(x) for x in src["line_items"]])
        rows = post_invoice(dup, ctx, self.ids)
        h.invoices.append(dup)
        h.ledger += rows
        self.used |= {src["invoice_id"], dup["invoice_id"]}
        changes = [{"table": "invoices", "record_id": dup["invoice_id"], "field": "__copy_of__",
                    "before": None, "after": src["invoice_id"]}] + _created("ledger", rows, dup["invoice_id"])
        iid = self._add("duplicate_invoice", [src["invoice_id"], dup["invoice_id"]], dup["total_minor"],
                        dup["currency"], [[src["doc_id"], dup["doc_id"]]], changes, scenario)
        return iid, dup

    # ---------- payment_allocation ----------

    def payment_allocation(self, scenario=None, payment=None):
        """По выписке платёж закрывает счёт целиком, а в книге на 1200 разнесена часть или ничего;
        остаток — на 2900. Возвращает issue_id или None."""
        h, ctx = self.h, self.h.ctx
        if payment is None:
            cands = [p for p in h.payments
                     if p["payment_id"] not in self.used and p["amount_minor"] > 0
                     and len(p["invoice_refs"]) == 1
                     and p["invoice_refs"][0]["invoice_id"] not in self.used]
            if not cands:
                return None
            payment = self.rng.choice(cands)
        pid = payment["payment_id"]
        ref = payment["invoice_refs"][0]
        total = ref["amount_minor"]
        if self.rng.randint(1, 4) == 1:
            allocated = 0  # не разнесено вовсе
        else:
            allocated = total * self.rng.randint(20, 80) // 100
        allocated = min(allocated, total - 1)
        by_id = {inv["invoice_id"]: inv for inv in h.invoices}
        old = [r for r in h.ledger if r["payment_id"] == pid]
        h.ledger = [r for r in h.ledger if r["payment_id"] != pid]
        new = post_payment(payment, ctx, self.ids, by_id, allocations={ref["invoice_id"]: allocated})
        h.ledger += new
        self.used |= {pid, ref["invoice_id"]}
        changes = ([{"table": "ledger", "record_id": pid, "field": f"allocated_minor:{ref['invoice_id']}",
                     "before": total, "after": allocated}]
                   + _deleted(old, pid) + _created("ledger", new, pid))
        evidence = [payment["doc_id"]] + [r["row_id"] for r in new]
        return self._add("payment_allocation", [ref["invoice_id"], pid], total - allocated,
                         payment["currency"], [evidence], changes, scenario)


def inject(history, cfg, rng, ids, counts) -> Injector:
    """Вносит запланированные ошибки. Если кандидатов не хватило — вносит, сколько получилось."""
    inj = Injector(history, rng, ids)
    for _ in range(counts["duplicate_invoice"]):
        inj.duplicate_invoice()
    for _ in range(counts["payment_allocation"]):
        inj.payment_allocation()
    # fx_conversion и period_cutoff — этап 3
    inj.issues.sort(key=lambda x: x["issue_id"])
    inj.mutations.sort(key=lambda x: x["issue_id"])
    return inj
