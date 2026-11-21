"""Отвлекающие случаи (контракт, раздел 11): законные записи, похожие на ошибки.

Строятся до ошибок, из своего потока RNG `distractors`, поэтому одинаковы в чистой
и в ошибочной версии конфига. Задействованные документы ошибки не трогают.
"""
from datetime import timedelta

from generator.config import DISTRACTORS
from generator.errors import Injector
from generator.history import MONTHS_RU, fmt_ts, post_invoice, post_payment


def plan_distractors(cfg, rng) -> dict:
    counts = {k: 0 for k in DISTRACTORS}
    for k in DISTRACTORS:
        toggle = cfg["distractors"][k]
        if toggle["enabled"]:
            counts[k] = rng.randint(*toggle["count"])
    return counts


def cap_distractors(counts, min_docs, n_errors) -> dict:
    """Мало документов: заказанные ошибки важнее отвлекающих случаев. Бюджет от нижней границы
    size.documents: ошибка занимает около 1,5 документа, отвлекающий случай - около 2 (замер:
    12 ошибок на 20-30 документах без ограничения теряли до трети ошибок). Лишние случаи
    снимаются по одному с самого частого вида. При настройках по умолчанию и в эпизодах
    репозитория (от 30 документов, до 10 ошибок) ограничение не срабатывает."""
    budget = max(0, (min_docs - 3 * n_errors // 2) // 2)
    counts = dict(counts)
    while sum(counts.values()) > budget:
        kind = max(DISTRACTORS, key=lambda k: counts[k])
        counts[kind] -= 1
    return counts


class DistractorMaker(Injector):
    """Пользуется помощниками Injector (_free, _linked, _repost) и общим множеством used."""

    def __init__(self, history, rng, ids):
        super().__init__(history, rng, ids)
        self.items = []

    def _item(self, kind, record_ids):
        self.items.append({"distractor_id": self.issue_ids.new("DST"), "kind": kind,
                           "record_ids": sorted(record_ids)})

    def _by_id(self):
        return {inv["invoice_id"]: inv for inv in self.h.invoices}

    def _time_on(self, day, hour_lo=9, hour_hi=18):
        return self.h.ctx.period_start.replace(year=day.year, month=day.month, day=day.day,
                                               hour=self.rng.randint(hour_lo, hour_hi),
                                               minute=self.rng.randint(0, 59))

    def _later_day(self, ts, max_days):
        """День через 1..max_days после ts, но строго до последнего дня периода включительно."""
        ctx = self.h.ctx
        day = ctx.local_date(ts)
        room = (ctx.period_end.date() - day).days - 1
        if room < 1:
            return None
        return day + timedelta(days=self.rng.randint(1, min(max_days, room)))

    def _reserved(self, doc):
        """Короткие счета оставлены дублям (в том числе валютным): отвлекающие случаи их не берут."""
        small = set(self.h.small_invoice_ids)
        if "payment_id" in doc:
            return any(r["invoice_id"] in small for r in doc["invoice_refs"])
        return doc["invoice_id"] in small

    def _single_payments(self):
        return [p for p in self.h.payments if p["amount_minor"] > 0 and len(p["invoice_refs"]) == 1
                and self._free(p) and not self._reserved(p) and self._later_day(p["timestamp"], 5)]

    def _new_payment(self, ts, currency, amount, invoice_id, ref_amount):
        pay = {"doc_id": self.ids.new("DOC"), "payment_id": self.ids.new("PAY"), "timestamp": fmt_ts(ts),
               "currency": currency, "amount_minor": amount,
               "invoice_refs": [{"invoice_id": invoice_id, "amount_minor": ref_amount}]}
        self.h.payments.append(pay)
        self.h.ledger += post_payment(pay, self.h.ctx, self.ids, self._by_id())
        return pay

    # ---------- split_payment ----------

    def split_payment(self):
        """Счёт закрыт двумя платежами; каждый разнесён полностью, сумма = сумме счёта."""
        ctx = self.h.ctx
        cands = self._single_payments()
        if not cands:
            return
        pay = self.rng.choice(cands)
        ref = pay["invoice_refs"][0]
        inv = self._by_id()[ref["invoice_id"]]
        total, day = ref["amount_minor"], ctx.local_date(inv["issued_at"])
        full = ctx.convert(total, pay["currency"], day)
        for _ in range(20):  # части подбираются так, чтобы 1200 по счёту закрылся без остатка
            part = total * self.rng.randint(30, 70) // 100
            if 0 < part < total and (ctx.convert(part, pay["currency"], day)
                                     + ctx.convert(total - part, pay["currency"], day)) == full:
                break
        else:
            return
        pay["amount_minor"] = part
        pay["invoice_refs"] = [{"invoice_id": inv["invoice_id"], "amount_minor": part}]
        self._repost(pay)
        second = self._new_payment(self._time_on(self._later_day(pay["timestamp"], 5)), pay["currency"],
                                   total - part, inv["invoice_id"], total - part)
        self.used |= self._linked(inv)
        self._item("split_payment", [inv["invoice_id"], pay["payment_id"], second["payment_id"]])

    # ---------- refund ----------

    def refund(self):
        """Возврат части или всей суммы после полной оплаты: отрицательный платёж, обратные проводки."""
        cands = self._single_payments()
        if not cands:
            return
        pay = self.rng.choice(cands)
        ref = pay["invoice_refs"][0]
        amount = ref["amount_minor"]
        if self.rng.randint(1, 4) != 1:
            amount = max(1, amount * self.rng.randint(10, 50) // 100)
        ts = self._time_on(self._later_day(pay["timestamp"], 7))
        back = self._new_payment(ts, pay["currency"], -amount, ref["invoice_id"], amount)
        self.used |= self._linked(pay) | {back["payment_id"]}
        self._item("refund", [ref["invoice_id"], pay["payment_id"], back["payment_id"]])

    # ---------- legit_recurring ----------

    def legit_recurring(self):
        """Тот же контрагент и та же сумма месяцем раньше, но другой период услуги в описании.

        Прошлый счёт и его проводки лежат в прошлом месяце (вне периода) — это законно.
        Берётся счёт в базовой валюте: курс на дату месяц назад не нужен.
        """
        h, ctx = self.h, self.h.ctx
        small = set(h.small_invoice_ids)
        cands = [inv for inv in h.invoices if inv["currency"] == ctx.base and inv["invoice_id"] in small
                 and self._free(inv)]
        if not cands:
            return
        cur = self.rng.choice(cands)
        issued = ctx.local_dt(cur["issued_at"])
        month, year = (issued.month - 1, issued.year) if issued.month > 1 else (12, issued.year - 1)
        prev_dt = issued.replace(year=year, month=month, day=min(issued.day, 28))
        old_label = f"{MONTHS_RU[issued.month - 1]} {issued.year}"
        new_label = f"{MONTHS_RU[month - 1]} {year}"
        lines = [dict(x, description=x["description"].replace(old_label, new_label)) for x in cur["line_items"]]
        prev = dict(cur, doc_id=self.ids.new("DOC"), invoice_id=self.ids.new("INV"), issued_at=fmt_ts(prev_dt),
                    due_at=fmt_ts(prev_dt + timedelta(days=30)), line_items=lines)
        h.invoices.append(prev)
        h.ledger += post_invoice(prev, ctx, self.ids)
        self.used |= self._linked(cur) | {prev["invoice_id"]}
        self._item("legit_recurring", [cur["invoice_id"], prev["invoice_id"]])

    # ---------- late_evening_local ----------

    def late_evening_local(self):
        """Документ в последний день периода в 23:xx по местному времени: он в периоде."""
        h, ctx = self.h, self.h.ctx
        paid = {r["invoice_id"] for p in h.payments for r in p["invoice_refs"]}
        invs = [i for i in h.invoices if i["invoice_id"] not in paid and self._free(i) and not self._reserved(i)]
        pays = [p for p in h.payments if p["amount_minor"] > 0 and self._free(p) and not self._reserved(p)]
        doc = self._pick_doc(invs, pays)
        if doc is None:
            return
        last_day = ctx.period_end.date() - timedelta(days=1)
        ts = fmt_ts(self._time_on(last_day, 23, 23))
        if "payment_id" in doc:
            doc["timestamp"] = ts
        else:
            doc["issued_at"] = ts
            doc["due_at"] = fmt_ts(ctx.local_dt(ts) + timedelta(days=30))
        self._repost(doc)
        self.used |= self._linked(doc)
        self._item("late_evening_local", [self._doc_key(doc)])


def make_distractors(history, rng, ids, counts) -> DistractorMaker:
    dm = DistractorMaker(history, rng, ids)
    for kind in DISTRACTORS:
        for _ in range(counts[kind]):
            getattr(dm, kind)()
    dm.items.sort(key=lambda x: x["distractor_id"])
    return dm
