"""Внесение ошибок в чистую историю (контракт, раздел 9).

Каждая ошибка — отдельный issue в truth.json и отдельная мутация в provenance.json.
Записи, задействованные одной ошибкой, другие ошибки не трогают: так пара
(type, affected_ids) уникальна, а доказательства не пересекаются.
"""
from datetime import timedelta
from decimal import Decimal

from generator.config import ERROR_TYPES
from generator.history import fmt_ts, post_invoice, post_payment
from generator.rng import IdFactory

TOLERANCE_MINOR = 1
MIN_FX_GAP_MINOR = 100  # расхождение FX много больше допуска: не путается с округлением


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


    # ---------- общие помощники ----------

    def _linked(self, doc):
        """ID документа и связанных с ним (счёт и его платежи): их не трогают другие ошибки."""
        if "payment_id" in doc:
            return {doc["payment_id"]} | {r["invoice_id"] for r in doc["invoice_refs"]}
        iid = doc["invoice_id"]
        return {iid} | {p["payment_id"] for p in self.h.payments
                        if any(r["invoice_id"] == iid for r in p["invoice_refs"])}

    def _free(self, doc):
        return not (self._linked(doc) & self.used)

    def _repost(self, doc, posted_at=None, rate=None):
        """Удаляет проводки документа и проводит его заново с переопределениями."""
        h, ctx = self.h, self.h.ctx
        if "payment_id" in doc:
            def mine(r):
                return r["payment_id"] == doc["payment_id"]
            by_id = {inv["invoice_id"]: inv for inv in h.invoices}
            old = [r for r in h.ledger if mine(r)]
            h.ledger = [r for r in h.ledger if not mine(r)]
            new = post_payment(doc, ctx, self.ids, by_id, posted_at=posted_at, rate=rate)
        else:
            def mine(r):
                return r["invoice_id"] == doc["invoice_id"] and r["payment_id"] is None
            old = [r for r in h.ledger if mine(r)]
            h.ledger = [r for r in h.ledger if not mine(r)]
            new = post_invoice(doc, ctx, self.ids, posted_at=posted_at, rate=rate)
        h.ledger += new
        return old, new

    @staticmethod
    def _doc_key(doc):
        return doc.get("payment_id") or doc["invoice_id"]

    def _pick_doc(self, invoices, payments):
        """Случайно счёт или платёж; если нужного вида нет, берётся другой вид."""
        first, second = (invoices, payments) if self.rng.randint(0, 1) == 0 else (payments, invoices)
        pool = first or second
        return self.rng.choice(pool) if pool else None

    # ---------- fx_conversion ----------

    def _wrong_rate(self, cur, day):
        """Курс другой даты (±1–5 дней) или искажённый на 1–5 %. Возвращает (курс, описание)."""
        ctx = self.h.ctx
        correct = ctx.rate(cur, day)
        if self.rng.randint(0, 1) == 0:
            shift = self.rng.choice((-1, 1)) * self.rng.randint(1, 5)
            other = day + timedelta(days=shift)
            return ctx.fx[(other, cur)], "rate_of:" + other.isoformat()
        pct = Decimal(self.rng.choice((-1, 1)) * self.rng.randint(100, 500)) / Decimal(10000)
        return (correct * (1 + pct)).quantize(correct), "distorted:" + str(pct)

    def fx_conversion(self, scenario=None, doc=None):
        """Проводка валютного документа в базовой валюте по курсу другой даты или искажённому."""
        h, ctx = self.h, self.h.ctx
        if doc is None:
            invs = [i for i in h.invoices if i["currency"] != ctx.base and self._free(i)]
            pays = [p for p in h.payments if p["currency"] != ctx.base and p["amount_minor"] > 0
                    and len(p["invoice_refs"]) == 1 and self._free(p)]
            doc = self._pick_doc(invs, pays)
            if doc is None:
                return None
        is_pay = "payment_id" in doc
        amount = doc["amount_minor"] if is_pay else doc["total_minor"]
        day = ctx.local_date(doc["timestamp"] if is_pay else doc["issued_at"])
        correct = ctx.convert(amount, doc["currency"], day)
        for _ in range(8):
            rate, how = self._wrong_rate(doc["currency"], day)
            posted = ctx.convert(amount, doc["currency"], day, rate)
            if abs(posted - correct) >= MIN_FX_GAP_MINOR:
                break
        else:
            return None
        key = self._doc_key(doc)
        self.used |= self._linked(doc)
        old, new = self._repost(doc, rate=rate)
        key_account = "1010" if is_pay else "1200"
        key_row = next(r for r in new if r["account"] == key_account and r["debit_minor"] > 0)
        fx_id = "FX:" + day.isoformat() + ":" + doc["currency"] + ":" + ctx.base
        changes = ([{"table": "ledger", "record_id": key_row["row_id"], "field": "debit_minor",
                     "before": correct, "after": posted},
                    {"table": "fx", "record_id": fx_id, "field": "rate_used",
                     "before": format(ctx.rate(doc["currency"], day), "f"),
                     "after": format(rate, "f") + " (" + how + ")"}]
                   + _deleted(old, key) + _created("ledger", new, key))
        return self._add("fx_conversion", [key], abs(posted - correct), ctx.base,
                         [[doc["doc_id"], key_row["row_id"], fx_id]], changes, scenario)

    # ---------- period_cutoff ----------

    def _outside_time(self, after_end=None):
        """Рабочее время вне периода: после конца (0–6 дней) или до начала (1–7 дней)."""
        ctx = self.h.ctx
        if after_end is None:
            after_end = self.rng.randint(0, 1) == 0
        if after_end:
            day = ctx.period_end.date() + timedelta(days=self.rng.randint(0, 6))
        else:
            day = ctx.period_start.date() - timedelta(days=self.rng.randint(1, 7))
        t = ctx.period_start.replace(year=day.year, month=day.month, day=day.day,
                                     hour=self.rng.randint(9, 18), minute=self.rng.randint(0, 59))
        return fmt_ts(t)

    def period_cutoff(self, scenario=None, doc=None, after_end=None):
        """Дата документа вне периода, а проводки остались внутри периода (по прежней дате)."""
        h = self.h
        if doc is None:
            paid = {r["invoice_id"] for p in h.payments for r in p["invoice_refs"]}
            # оплаченный счёт после конца периода дал бы платёж раньше счёта: берём неоплаченные
            invs = [i for i in h.invoices if i["invoice_id"] not in paid and self._free(i)]
            pays = [p for p in h.payments if p["amount_minor"] > 0 and self._free(p)]
            doc = self._pick_doc(invs, pays)
            if doc is None:
                return None
        is_pay = "payment_id" in doc
        field = "timestamp" if is_pay else "issued_at"
        posted_at = doc[field]
        new_ts = self._outside_time(after_end=True if is_pay else after_end)
        doc[field] = new_ts
        if not is_pay:
            doc["due_at"] = fmt_ts(h.ctx.local_dt(new_ts) + timedelta(days=30))
        key = self._doc_key(doc)
        self.used |= self._linked(doc)
        old, new = self._repost(doc, posted_at=posted_at)
        changes = ([{"table": "payments" if is_pay else "invoices", "record_id": key, "field": field,
                     "before": posted_at, "after": new_ts}]
                   + _deleted(old, key) + _created("ledger", new, key))
        evidence = [[doc["doc_id"], r["row_id"]] for r in sorted(new, key=lambda r: r["row_id"])]
        amount = doc["amount_minor"] if is_pay else doc["total_minor"]
        return self._add("period_cutoff", [key], abs(amount), doc["currency"], evidence, changes, scenario)


def inject(history, cfg, rng, ids, counts) -> Injector:
    """Вносит запланированные ошибки. Если кандидатов не хватило — вносит, сколько получилось."""
    inj = Injector(history, rng, ids)
    for _ in range(counts["duplicate_invoice"]):
        inj.duplicate_invoice()
    for _ in range(counts["payment_allocation"]):
        inj.payment_allocation()
    for _ in range(counts["fx_conversion"]):
        inj.fx_conversion()
    for _ in range(counts["period_cutoff"]):
        inj.period_cutoff()
    inj.issues.sort(key=lambda x: x["issue_id"])
    inj.mutations.sort(key=lambda x: x["issue_id"])
    return inj
