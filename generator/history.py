"""Чистая история компании: курсы, контрагенты, счета, платежи и проводки (контракт, раздел 4).

Деньги — целые minor units, курсы — Decimal из той же строки, что попадёт в fx.csv.
Функции проводок принимают переопределения (время проводки, курс) — через них
следующие этапы вносят ошибки, не дублируя логику учёта.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal

# Сколько KZT стоит 1 единица валюты (стартовое значение случайного блуждания)
KZT_VALUE = {
    "KZT": Decimal("1"), "USD": Decimal("470"), "EUR": Decimal("510"), "CNY": Decimal("65"),
    "RUB": Decimal("5.2"), "JPY": Decimal("3.1"), "GBP": Decimal("600"), "TRY": Decimal("14"),
    "KGS": Decimal("5.4"), "UZS": Decimal("0.037"),
}
EXPONENTS = {"JPY": 0, "KRW": 0, "KWD": 3, "BHD": 3}  # остальные — 2
FX_MARGIN_DAYS = 10

MONTHS_RU = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
             "сентябрь", "октябрь", "ноябрь", "декабрь")
GOODS = {
    "importer": ("Ноутбуки", "Смартфоны", "Мониторы", "Сетевое оборудование",
                 "Комплектующие для ПК", "Принтеры и МФУ", "Серверное оборудование"),
    "saas": ("Подписка CRM, тариф Бизнес", "Облачное хранилище, 1 ТБ", "Лицензия модуля аналитики",
             "Техническая поддержка", "Внедрение и настройка", "Дополнительные места пользователей"),
    "distributor": ("Бытовая химия", "Канцелярские товары", "Упаковочные материалы", "Напитки",
                    "Продукты длительного хранения", "Хозяйственные товары"),
}
# Доля контрагентов в иностранной валюте
FOREIGN_SHARE = {"importer": 70, "saas": 20, "distributor": 40}


def exponent(currency: str) -> int:
    return EXPONENTS.get(currency, 2)


def parse_tz(offset: str) -> timezone:
    sign = 1 if offset[0] == "+" else -1
    hours, minutes = int(offset[1:3]), int(offset[4:6])
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def fmt_ts(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


@dataclass
class Context:
    """Общие параметры эпизода и правила конвертации из policy.json."""
    base: str
    tz: timezone
    tz_name: str
    period_start: datetime
    period_end: datetime
    exps: dict
    fx: dict = field(default_factory=dict)          # (date, currency) -> Decimal
    fx_str: dict = field(default_factory=dict)      # (date, currency) -> строка курса

    def local_date(self, ts: str) -> date:
        return datetime.fromisoformat(ts).astimezone(self.tz).date()

    def in_period(self, ts: str) -> bool:
        return self.period_start <= datetime.fromisoformat(ts) < self.period_end

    def rate(self, currency: str, day: date) -> Decimal:
        return Decimal(1) if currency == self.base else self.fx[(day, currency)]

    def convert(self, amount_minor: int, currency: str, day: date, rate: Decimal = None) -> int:
        """amount × курс(день), ROUND_HALF_EVEN до разрядности базовой валюты."""
        if currency == self.base and rate is None:
            return amount_minor
        r = self.rate(currency, day) if rate is None else rate
        shift = Decimal(10) ** (self.exps[self.base] - self.exps[currency])
        value = Decimal(amount_minor) * r * shift
        return int(value.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


@dataclass
class History:
    ctx: Context
    episode_id: str
    entities: list
    invoices: list
    payments: list
    ledger: list
    fx_rows: list
    small_invoice_ids: list = field(default_factory=list)  # короткие счета: кандидаты в дубли


# ---------- проводки ----------

def _row(ids, ctx, ts, account, debit, credit, entity_id, invoice_id, payment_id) -> dict:
    assert (debit > 0) != (credit > 0), "в строке ровно одна сторона > 0"
    return {"row_id": ids.new("L"), "timestamp": ts, "account": account,
            "debit_minor": debit, "credit_minor": credit, "currency": ctx.base,
            "entity_id": entity_id, "invoice_id": invoice_id, "payment_id": payment_id}


def post_invoice(inv, ctx, ids, posted_at=None, rate=None) -> list:
    """Дт 1200 на total × курс(дата счёта); Кт 4000 по строкам, последняя забирает округление."""
    ts = posted_at or inv["issued_at"]
    day = ctx.local_date(inv["issued_at"])
    total_b = ctx.convert(inv["total_minor"], inv["currency"], day, rate)
    ent, iid = inv["entity_id"], inv["invoice_id"]
    rows = [_row(ids, ctx, ts, "1200", total_b, 0, ent, iid, None)]
    credited = 0
    items = inv["line_items"]
    for i, item in enumerate(items):
        if i < len(items) - 1:
            part = ctx.convert(item["amount_minor"], inv["currency"], day, rate)
        else:
            part = total_b - credited
        if part <= 0:
            raise ValueError("строка счёта после конвертации <= 0")
        credited += part
        rows.append(_row(ids, ctx, ts, "4000", 0, part, ent, iid, None))
    return rows


def post_payment(pay, ctx, ids, invoices_by_id, posted_at=None, rate=None, allocations=None) -> list:
    """Дт 1010 на amount × курс(дата платежа); Кт 1200 по каждому invoice_ref по курсу даты счёта;
    разница — 7900; нераспределённый остаток — Кт 2900.

    allocations — сколько из каждого ref реально разнесено в книге (по умолчанию всё).
    """
    ts = posted_at or pay["timestamp"]
    cur = pay["currency"]
    cash = ctx.convert(pay["amount_minor"], cur, ctx.local_date(pay["timestamp"]), rate)
    refs = pay["invoice_refs"]
    ent = invoices_by_id[refs[0]["invoice_id"]]["entity_id"] if refs else None
    pid = pay["payment_id"]
    rows = [_row(ids, ctx, ts, "1010", cash, 0, ent, None, pid)]
    # Формула совпадает с grader/evidence.payment_book: 2900 = остаток × курс платежа,
    # 7900 = всё, что не сошлось (курсовая разница).
    credited_ar = 0
    allocated_total = 0
    for ref in refs:
        amount = ref["amount_minor"] if allocations is None else allocations[ref["invoice_id"]]
        allocated_total += amount
        if amount <= 0:
            continue
        inv = invoices_by_id[ref["invoice_id"]]
        ar = ctx.convert(amount, cur, ctx.local_date(inv["issued_at"]))
        rows.append(_row(ids, ctx, ts, "1200", 0, ar, ent, ref["invoice_id"], pid))
        credited_ar += ar
    remainder = pay["amount_minor"] - allocated_total
    unallocated = ctx.convert(remainder, cur, ctx.local_date(pay["timestamp"]), rate) if remainder else 0
    if unallocated > 0:
        rows.append(_row(ids, ctx, ts, "2900", 0, unallocated, ent, None, pid))
    diff = cash - credited_ar - unallocated  # > 0 — курсовой доход, < 0 — расход
    if diff > 0:
        rows.append(_row(ids, ctx, ts, "7900", 0, diff, ent, None, pid))
    elif diff < 0:
        rows.append(_row(ids, ctx, ts, "7900", -diff, 0, ent, None, pid))
    return rows


# ---------- построение ----------

def _period(rng, tz):
    month = rng.randint(1, 12)
    start = datetime(2026, month, 1, tzinfo=tz)
    end = datetime(2027, 1, 1, tzinfo=tz) if month == 12 else datetime(2026, month + 1, 1, tzinfo=tz)
    return start, end


def _build_fx(rng, ctx, currencies):
    """Курс на каждый день от period_start − 10 до period_end + 10 для каждой иностранной валюты."""
    first = ctx.period_start.date() - timedelta(days=FX_MARGIN_DAYS)
    last = ctx.period_end.date() + timedelta(days=FX_MARGIN_DAYS)
    value = {}
    for cur in [ctx.base] + currencies:
        value[cur] = KZT_VALUE.get(cur) or Decimal(rng.randint(1, 500))
    rows = []
    day = first
    while day <= last:
        for cur in [ctx.base] + currencies:
            if cur != "KZT":
                value[cur] *= Decimal(1) + Decimal(rng.randint(-40, 40)) / Decimal(10000)
        for cur in currencies:
            r = value[cur] / value[ctx.base]
            q = Decimal("0.0001") if r >= 1 else Decimal("0.00000001")
            text = format(r.quantize(q, rounding=ROUND_HALF_EVEN), "f")
            ctx.fx[(day, cur)] = Decimal(text)
            ctx.fx_str[(day, cur)] = text
            rows.append({"date": day.isoformat(), "source_currency": cur,
                         "target_currency": ctx.base, "rate": text})
        day += timedelta(days=1)
    return rows


def _work_time(rng, day: date, tz) -> datetime:
    """Рабочее время 09:00–18:59: поздний вечер оставлен отвлекающему случаю."""
    return datetime(day.year, day.month, day.day, rng.randint(9, 18), rng.randint(0, 59), 0, tzinfo=tz)


def plan_sizes(rng, cfg, reserve_rows=0, reserve_docs=0):
    """Число документов и целевое число проводок с учётом совместимости диапазонов."""
    d_lo, d_hi = cfg["size"]["documents"]
    r_lo, r_hi = cfg["size"]["ledger_rows"]
    r_hi_eff = max(r_lo, r_hi - reserve_rows)
    docs = rng.randint(d_lo, max(d_lo, d_hi - reserve_docs))
    while True:
        n_pay = docs * 2 // 5
        n_inv = docs - n_pay
        rows_min = 2 * n_inv + 3 * n_pay
        if rows_min <= r_hi_eff or docs <= 20:
            break
        docs -= 1  # диапазоны несовместимы: меньше документов, чтобы уложиться в строки
    # +3: запас на редкие платежи без курсовой разницы (у них на строку меньше)
    lo = max(min(r_lo + 3, r_hi_eff), rows_min)
    hi = max(lo, r_lo + (r_hi_eff - r_lo) * 3 // 4)
    return n_inv, n_pay, rng.randint(lo, hi)


def build_clean(cfg, content, ids, reserve_rows=0, reserve_docs=0, small_count=0) -> History:
    """small_count — сколько счетов оставить с 1–2 строками (дубль такого счёта не раздует книгу)."""
    company = cfg["company"]
    tz = parse_tz(company["timezone"])
    base = company["base_currency"]
    foreign = list(company["foreign_currencies"])
    start, end = _period(content, tz)
    exps = {c: exponent(c) for c in [base] + foreign}
    ctx = Context(base=base, tz=tz, tz_name=company["timezone"], period_start=start,
                  period_end=end, exps=exps)
    episode_id = ids.new("EP")
    fx_rows = _build_fx(content, ctx, foreign)

    # контрагенты
    share = FOREIGN_SHARE[company["archetype"]] if foreign else 0
    entities = []
    for _ in range(content.randint(6, 14)):
        cur = content.choice(foreign) if content.randint(1, 100) <= share else base
        entities.append({"entity_id": ids.new("E"), "currency": cur})

    n_inv, n_pay, target_rows = plan_sizes(content, cfg, reserve_rows, reserve_docs)
    days = (end.date() - start.date()).days
    goods = GOODS[company["archetype"]]
    period_label = f"{MONTHS_RU[start.month - 1]} {start.year}"

    # даты и контрагенты счетов; первые n_pay будут оплачены, их даты не у самого конца периода
    drafts = []
    for i in range(n_inv):
        day = start.date() + timedelta(days=content.randint(0, days - 3 if i < n_pay else days - 1))
        drafts.append({"issued": _work_time(content, day, tz), "entity": content.choice(entities),
                       "paid": i < n_pay, "lines": 1})
    pay_rows = sum(2 if d["entity"]["currency"] == base else 3 for d in drafts if d["paid"])
    small = set(content.sample(range(n_inv), min(small_count, n_inv - 1))) if small_count else set()
    for i in sorted(small):
        drafts[i]["lines"] = content.randint(1, 2)
    growable = [d for i, d in enumerate(drafts) if i not in small]
    extra_lines = max(0, target_rows - pay_rows - 2 * n_inv - sum(d["lines"] - 1 for d in drafts))
    for _ in range(extra_lines):
        content.choice(growable)["lines"] += 1

    invoices, payments = [], []
    for d in drafts:
        cur = d["entity"]["currency"]
        lines = []
        for _ in range(d["lines"]):
            kzt_unit = Decimal(content.randint(2_000, 300_000))
            unit = int((kzt_unit / KZT_VALUE.get(cur, Decimal(100)) * Decimal(10) ** exps[cur])
                       .quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
            unit = max(unit, 100)
            qty = content.randint(1, 12)
            lines.append({"description": f"{content.choice(goods)}, {period_label}",
                          "quantity": qty, "unit_price_minor": unit, "amount_minor": qty * unit})
        invoices.append({
            "doc_id": ids.new("DOC"), "invoice_id": ids.new("INV"),
            "entity_id": d["entity"]["entity_id"], "issued_at": fmt_ts(d["issued"]),
            "due_at": fmt_ts(d["issued"] + timedelta(days=30)), "currency": cur,
            "total_minor": sum(x["amount_minor"] for x in lines), "line_items": lines})

    for d, inv in zip(drafts, invoices):
        if not d["paid"]:
            continue
        max_delta = (end.date() - d["issued"].date()).days - 1
        pay_day = d["issued"].date() + timedelta(days=content.randint(1, min(15, max_delta)))
        payments.append({
            "doc_id": ids.new("DOC"), "payment_id": ids.new("PAY"),
            "timestamp": fmt_ts(_work_time(content, pay_day, tz)), "currency": inv["currency"],
            "amount_minor": inv["total_minor"],
            "invoice_refs": [{"invoice_id": inv["invoice_id"], "amount_minor": inv["total_minor"]}]})

    by_id = {inv["invoice_id"]: inv for inv in invoices}
    small_ids = [invoices[i]["invoice_id"] for i in sorted(small)]
    ledger = []
    for inv in invoices:
        ledger += post_invoice(inv, ctx, ids)
    for pay in payments:
        ledger += post_payment(pay, ctx, ids, by_id)
    return History(ctx=ctx, episode_id=episode_id, entities=entities, invoices=invoices,
                   payments=payments, ledger=ledger, fx_rows=fx_rows, small_invoice_ids=small_ids)
