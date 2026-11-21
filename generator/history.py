"""Чистая история компании: курсы, контрагенты, счета, платежи и проводки (контракт, раздел 4).

Деньги — целые minor units, курсы — Decimal из той же строки, что попадёт в fx.csv.
Функции проводок принимают переопределения (время проводки, курс) — через них
следующие этапы вносят ошибки, не дублируя логику учёта.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal

from generator.config import DOCUMENT_BOUNDS, LEDGER_BOUNDS

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
    start_value: dict = field(default_factory=dict)  # currency -> стартовая стоимость в KZT (для цен)

    def local_date(self, ts: str) -> date:
        return datetime.fromisoformat(ts).astimezone(self.tz).date()

    def local_dt(self, ts: str) -> datetime:
        return datetime.fromisoformat(ts).astimezone(self.tz)

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
    Возврат (amount_minor < 0) — обратные проводки: Кт 1010, Дт 1200.
    Формула совпадает с grader/evidence.payment_book: значения со знаком (дебет − кредит),
    2900 = остаток × курс платежа, 7900 = всё, что не сошлось (курсовая разница).
    """
    ts = posted_at or pay["timestamp"]
    cur = pay["currency"]
    pay_day = ctx.local_date(pay["timestamp"])
    sign = 1 if pay["amount_minor"] >= 0 else -1
    refs = pay["invoice_refs"]
    ent = invoices_by_id[refs[0]["invoice_id"]]["entity_id"] if refs else None
    book = [("1010", None, ctx.convert(pay["amount_minor"], cur, pay_day, rate))]
    allocated_total = 0
    for ref in refs:
        amount = ref["amount_minor"] if allocations is None else allocations[ref["invoice_id"]]
        allocated_total += amount
        if amount > 0:
            inv = invoices_by_id[ref["invoice_id"]]
            ar = ctx.convert(amount, cur, ctx.local_date(inv["issued_at"]))
            book.append(("1200", ref["invoice_id"], -sign * ar))
    remainder = abs(pay["amount_minor"]) - allocated_total
    book.append(("2900", None, -sign * ctx.convert(remainder, cur, pay_day, rate)))
    book.append(("7900", None, -sum(v for _, _, v in book)))
    return [_row(ids, ctx, ts, account, max(v, 0), max(-v, 0), ent, iid, pay["payment_id"])
            for account, iid, v in book if v != 0]


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
    # цены счетов считаются от той же стоимости, что и курсы (иначе для валют вне KZT_VALUE
    # суммы в базовой валюте расходились бы в разы)
    ctx.start_value = dict(value)
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
    # Запас под ошибки и отвлекающие случаи вычитается из ВЕРХНЕЙ границы и не возвращается
    # нижней: иначе при узком диапазоне (например [2000, 2000]) внесённые строки и документы
    # выходят за предел кейса. Если запас не помещается, чистая часть может быть меньше
    # нижней границы, итог всё равно близок к верхней. При обычных диапазонах выбор прежний.
    r_hi_eff = max(LEDGER_BOUNDS[0], r_hi - reserve_rows)
    # Внесённые строки иногда превышают запас (до +1 на каждые 3 зарезервированные), а чистая
    # история может перебрать цель на 3 строки. При широком диапазоне цель берётся не выше 3/4
    # диапазона и этого хватает; при узком цель прижата к верхней границе - нужен явный допуск.
    slack = reserve_rows // 3 + 4
    if (r_hi_eff - r_lo) // 4 < slack:
        r_hi_eff = max(LEDGER_BOUNDS[0], r_hi - reserve_rows - slack)
    d_hi_eff = max(DOCUMENT_BOUNDS[0], d_hi - reserve_docs)
    docs = rng.randint(min(d_lo, d_hi_eff), d_hi_eff)
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


def _make_entities(content, ids, archetype, base, foreign, need_foreign=False, need_recoverable=None):
    share = FOREIGN_SHARE[archetype] if foreign else 0
    entities = []
    for _ in range(content.randint(6, 14)):
        cur = content.choice(foreign) if content.randint(1, 100) <= share else base
        entities.append({"entity_id": ids.new("E"), "currency": cur})
    if all(e["currency"] != base for e in entities):
        entities[0]["currency"] = base  # хотя бы один контрагент в базовой валюте
    if need_foreign and foreign and all(e["currency"] == base for e in entities):
        # совмещённым сценариям нужен валютный контрагент (у saas их мало); rng тратится
        # только здесь, поэтому эпизоды, где валютный контрагент и так есть, не меняются
        entities[content.randrange(1, len(entities))]["currency"] = content.choice(foreign)
    if need_recoverable and not any(e["currency"] in need_recoverable for e in entities):
        # под partial_fx_payment_at_cutoff нужен контрагент в восстановимой валюте: им становится
        # один из валютных (он есть после шага выше), контрагенты в базовой валюте не трогаются
        idx = [i for i, e in enumerate(entities) if e["currency"] != base]
        entities[content.choice(idx)]["currency"] = content.choice(need_recoverable)
    return entities


def _recoverable_currency(ctx, cur) -> bool:
    """1 minor валюты стоит не меньше 1.1 minor базовой: частичную оплату можно восстановить
    из проводки (тот же критерий, что Injector._recoverable, с запасом на блуждание курса)."""
    rate = ctx.rate(cur, ctx.period_start.date())
    return rate * Decimal(10) ** (ctx.exps[ctx.base] - ctx.exps[cur]) >= Decimal("1.1")


def _reserve_foreign_paid(content, ctx, drafts, small, entities, need):
    """Минимум need оплаченных счетов у валютных контрагентов (под partial_fx_payment_at_cutoff).
    Меняет только оплаченные счета вне коротких; если их хватает, rng не трогает."""
    good = [e for e in entities if e["currency"] != ctx.base and _recoverable_currency(ctx, e["currency"])]
    if not good:  # восстановимой валюты нет: сценарий внесётся полным пропуском в любой валюте
        good = [e for e in entities if e["currency"] != ctx.base]
    if not need or not good:
        return
    have = sum(1 for d in drafts if d["paid"] and d["entity"] in good)
    for i, d in enumerate(drafts):
        if have >= need:
            break
        if d["paid"] and i not in small and d["entity"]["currency"] == ctx.base:
            d["entity"] = content.choice(good)
            have += 1


def _reserve_recoverable_paid(content, ctx, drafts, small, entities, need, keep_foreign):
    """Минимум need оплаченных счетов в базовой или восстановимой валюте (под payment_allocation:
    частичную оплату восстанавливают по проводке 1200). Если их мало, оплаченные счета в дешёвой
    валюте переходят к контрагенту в базовой валюте, но keep_foreign из них остаются (под сценарий 1).
    Если таких счетов хватает, rng не трогает."""
    def good(e):
        return e["currency"] == ctx.base or _recoverable_currency(ctx, e["currency"])
    have = sum(1 for d in drafts if d["paid"] and good(d["entity"]))
    if have >= need:
        return
    base = [e for e in entities if e["currency"] == ctx.base]
    cheap = [i for i, d in enumerate(drafts) if d["paid"] and i not in small and not good(d["entity"])]
    for i in cheap[:max(0, len(cheap) - keep_foreign)]:
        if have >= need:
            break
        drafts[i]["entity"] = content.choice(base)
        have += 1


def _reserve_foreign_docs(content, drafts, small, foreign_entities, need):
    """Минимум need счетов у валютных контрагентов (под fx_conversion). Меняет только счета
    вне коротких; если валютных счетов хватает, rng не трогает."""
    have = sum(1 for d in drafts if d["entity"] in foreign_entities)
    if not foreign_entities or have >= need:
        return
    for i in content.sample(range(len(drafts)), len(drafts)):
        if have >= need:
            break
        if i not in small and drafts[i]["entity"] not in foreign_entities:
            drafts[i]["entity"] = content.choice(foreign_entities)
            have += 1


def _make_drafts(content, ctx, entities, n_inv, n_pay, target_rows, small_count, base_small=0,
                 foreign_small=0, foreign_paid=0, foreign_docs=0, recoverable_paid=0):
    """Даты, контрагенты и число строк счетов. Первые n_pay будут оплачены — их даты
    не у самого конца периода. Короткие счета (1–2 строки) есть всегда, не только у дублей,
    чтобы краткость счёта не выдавала ошибку."""
    days = (ctx.period_end.date() - ctx.period_start.date()).days
    drafts = []
    for i in range(n_inv):
        day = ctx.period_start.date() + timedelta(days=content.randint(0, days - 3 if i < n_pay else days - 1))
        drafts.append({"issued": _work_time(content, day, ctx.tz), "entity": content.choice(entities),
                       "paid": i < n_pay, "lines": 1})
    n_small = min(max(small_count, n_inv // 6), n_inv - 1)
    small = set(content.sample(range(n_inv), n_small))
    base_entities = [e for e in entities if e["currency"] == ctx.base]
    foreign_entities = [e for e in entities if e["currency"] != ctx.base]
    for k, i in enumerate(sorted(small)):
        drafts[i]["lines"] = content.randint(1, 2)
        if k < base_small:  # под повторяющуюся услугу: прошлый месяц без курса валюты
            drafts[i]["entity"] = content.choice(base_entities)
        elif k < base_small + foreign_small and foreign_entities:  # под дубль с неверным курсом
            drafts[i]["entity"] = content.choice(foreign_entities)
    _reserve_foreign_paid(content, ctx, drafts, small, entities, foreign_paid)
    _reserve_foreign_docs(content, drafts, small, foreign_entities, foreign_docs)
    _reserve_recoverable_paid(content, ctx, drafts, small, entities, recoverable_paid, foreign_paid)
    pay_rows = sum(2 if d["entity"]["currency"] == ctx.base else 3 for d in drafts if d["paid"])
    growable = [d for i, d in enumerate(drafts) if i not in small]
    extra_lines = max(0, target_rows - pay_rows - 2 * n_inv - sum(d["lines"] - 1 for d in drafts))
    for _ in range(extra_lines):
        content.choice(growable)["lines"] += 1
    return drafts, sorted(small)


def _make_invoice(content, ids, ctx, draft, goods, period_label):
    cur = draft["entity"]["currency"]
    lines = []
    for _ in range(draft["lines"]):
        kzt_unit = Decimal(content.randint(2_000, 300_000))
        unit = int((kzt_unit / ctx.start_value.get(cur, KZT_VALUE.get(cur, Decimal(100)))
                    * Decimal(10) ** ctx.exps[cur])
                   .quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
        qty = content.randint(1, 12)
        unit = max(unit, 100)
        lines.append({"description": f"{content.choice(goods)}, {period_label}",
                      "quantity": qty, "unit_price_minor": unit, "amount_minor": qty * unit})
    return {"doc_id": ids.new("DOC"), "invoice_id": ids.new("INV"),
            "entity_id": draft["entity"]["entity_id"], "issued_at": fmt_ts(draft["issued"]),
            "due_at": fmt_ts(draft["issued"] + timedelta(days=30)), "currency": cur,
            "total_minor": sum(x["amount_minor"] for x in lines), "line_items": lines}


def _make_payment(content, ids, ctx, draft, inv):
    """Полная оплата счёта через 1–15 дней, внутри периода."""
    max_delta = (ctx.period_end.date() - draft["issued"].date()).days - 1
    pay_day = draft["issued"].date() + timedelta(days=content.randint(1, min(15, max_delta)))
    return {"doc_id": ids.new("DOC"), "payment_id": ids.new("PAY"),
            "timestamp": fmt_ts(_work_time(content, pay_day, ctx.tz)), "currency": inv["currency"],
            "amount_minor": inv["total_minor"],
            "invoice_refs": [{"invoice_id": inv["invoice_id"], "amount_minor": inv["total_minor"]}]}


def build_clean(cfg, content, ids, reserve_rows=0, reserve_docs=0, small_count=0, base_small=0,
                foreign_small=0, foreign_paid=0, foreign_docs=0, recoverable_paid=0) -> History:
    """small_count — сколько счетов минимум оставить с 1–2 строками (кандидаты в дубли);
    base_small — сколько из них сделать в базовой валюте (под legit_recurring);
    foreign_small — сколько в иностранной (под duplicate_invoice_wrong_fx);
    foreign_paid — сколько оплаченных валютных счетов нужно (под partial_fx_payment_at_cutoff);
    foreign_docs — сколько валютных счетов нужно (под fx_conversion);
    recoverable_paid — сколько оплаченных счетов в базовой или восстановимой валюте (под payment_allocation)."""
    company = cfg["company"]
    tz = parse_tz(company["timezone"])
    base = company["base_currency"]
    foreign = list(company["foreign_currencies"])
    start, end = _period(content, tz)
    ctx = Context(base=base, tz=tz, tz_name=company["timezone"], period_start=start,
                  period_end=end, exps={c: exponent(c) for c in [base] + foreign})
    episode_id = ids.new("EP")
    fx_rows = _build_fx(content, ctx, foreign)
    entities = _make_entities(content, ids, company["archetype"], base, foreign,
                              need_foreign=bool(foreign_small or foreign_paid or foreign_docs),
                              need_recoverable=[c for c in foreign if _recoverable_currency(ctx, c)]
                              if foreign_paid else None)
    n_inv, n_pay, target_rows = plan_sizes(content, cfg, reserve_rows, reserve_docs)
    drafts, small = _make_drafts(content, ctx, entities, n_inv, n_pay, target_rows, small_count,
                                 base_small, foreign_small, foreign_paid, foreign_docs, recoverable_paid)

    goods = GOODS[company["archetype"]]
    period_label = f"{MONTHS_RU[start.month - 1]} {start.year}"
    invoices = [_make_invoice(content, ids, ctx, d, goods, period_label) for d in drafts]
    payments = [_make_payment(content, ids, ctx, d, inv) for d, inv in zip(drafts, invoices) if d["paid"]]

    by_id = {inv["invoice_id"]: inv for inv in invoices}
    ledger = []
    for inv in invoices:
        ledger += post_invoice(inv, ctx, ids)
    for pay in payments:
        ledger += post_payment(pay, ctx, ids, by_id)
    return History(ctx=ctx, episode_id=episode_id, entities=entities, invoices=invoices,
                   payments=payments, ledger=ledger, fx_rows=fx_rows,
                   small_invoice_ids=[invoices[i]["invoice_id"] for i in small])
