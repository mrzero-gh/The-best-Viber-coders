"""Политика на основе правил аудита (Rule-based / Heuristic Auditor)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from fractions import Fraction
from policies.base import Policy


def _parse_tz(offset_str: str) -> timezone:
    sign = 1 if offset_str[0] == "+" else -1
    hours = int(offset_str[1:3])
    minutes = int(offset_str[4:6])
    return timezone(sign * timedelta(minutes=hours * 60 + minutes))


MAX_FINDINGS = 30  # docs/contract.md, таблица констант


def _limit_findings(findings: list, limit: int = MAX_FINDINGS) -> list:
    """Не больше limit выводов. Если их больше, берём по очереди из каждого типа, чтобы
    срез [:limit] не отбрасывал целиком тип, проверяемый последним (fx_conversion)."""
    if len(findings) <= limit:
        return findings
    by_type = {}
    for f in findings:
        by_type.setdefault(f["type"], []).append(f)
    queues = list(by_type.values())
    out = []
    while len(out) < limit:
        for q in queues:
            if q and len(out) < limit:
                out.append(q.pop(0))
    return out


class RuleBasedPolicy(Policy):
    """Политика аудита на основе правил.

    Последовательно запрашивает открытые таблицы (invoices, payments, ledger, fx)
    через list_records (и при необходимости get_document), после чего применяет
    правила бухгалтерского аудита:
    1. duplicate_invoice — дубликаты счетов в пределах 5 дней, проведенные в книге;
    2. period_cutoff — документы вне отчётного периода, проведённые внутри периода;
    3. payment_allocation — неполное распределение платежа (остаток на счёте 2900);
    4. fx_conversion — неверный курс или дата валютной конвертации.
    """

    def __init__(self, naive: bool = False):
        self.naive = naive
        self.reset()

    def reset(self) -> None:
        self.state = "START"
        self.manifest = None
        self.policy_rules = None
        self.episode_id = "EP-00000000"
        self.invoices = []
        self.payments = []
        self.ledger = []
        self.fx = {}
        self.line_items_by_doc_id = {}
        self.duplicate_candidates = []
        self.pending_docs_to_fetch = []
        self._submitted = False

    def act(self, observation: dict) -> dict:
        kind = observation.get("kind")
        remaining = observation.get("steps_remaining", 100)

        # Защита от исчерпания шагов
        if remaining <= 3 and not self._submitted:
            findings = self._audit()
            self._submitted = True
            return {"tool": "submit", "args": {"findings": {"episode_id": self.episode_id, "findings": findings}}}

        if kind == "initial":
            self.manifest = observation["manifest"]
            self.policy_rules = observation.get("policy", {})
            self.episode_id = self.manifest["episode_id"]
            self.state = "FETCH_INVOICES"
            return {"tool": "list_records", "args": {"table": "invoices", "limit": 50}}

        if kind == "result":
            tool = observation.get("tool")
            res = observation.get("result", {})

            if tool == "list_records":
                table = res.get("table")
                records = res.get("records", [])
                cursor = res.get("next_cursor")

                if table == "invoices":
                    self.invoices.extend(records)
                    if cursor:
                        return {"tool": "list_records", "args": {"table": "invoices", "cursor": cursor, "limit": 50}}
                    return {"tool": "list_records", "args": {"table": "payments", "limit": 50}}

                elif table == "payments":
                    self.payments.extend(records)
                    if cursor:
                        return {"tool": "list_records", "args": {"table": "payments", "cursor": cursor, "limit": 50}}
                    return {"tool": "list_records", "args": {"table": "fx", "limit": 50}}

                elif table == "fx":
                    for r in records:
                        k = f"FX:{r['date']}:{r['source_currency']}:{r['target_currency']}"
                        self.fx[k] = r
                    if cursor:
                        return {"tool": "list_records", "args": {"table": "fx", "cursor": cursor, "limit": 50}}
                    return {"tool": "list_records", "args": {"table": "ledger", "limit": 50}}

                elif table == "ledger":
                    for r in records:
                        r["debit_minor"] = int(r["debit_minor"])
                        r["credit_minor"] = int(r["credit_minor"])
                    self.ledger.extend(records)
                    if cursor:
                        return {"tool": "list_records", "args": {"table": "ledger", "cursor": cursor, "limit": 50}}

                    # Все 4 таблицы загружены
                    candidates = self._find_duplicate_doc_ids()
                    if not self.naive and candidates and remaining > len(candidates) + 5:
                        # sorted: порядок не должен зависеть от PYTHONHASHSEED (воспроизводимость траекторий)
                        self.pending_docs_to_fetch = sorted(candidates)
                        next_doc = self.pending_docs_to_fetch.pop(0)
                        return {"tool": "get_document", "args": {"doc_id": next_doc}}
                    else:
                        findings = self._audit()
                        self._submitted = True
                        return {"tool": "submit", "args": {"findings": {"episode_id": self.episode_id, "findings": findings}}}

            elif tool == "get_document":
                doc = res.get("document", {})
                if doc.get("doc_id"):
                    self.line_items_by_doc_id[doc["doc_id"]] = doc.get("line_items", [])
                if self.pending_docs_to_fetch and remaining > len(self.pending_docs_to_fetch) + 3:
                    next_doc = self.pending_docs_to_fetch.pop(0)
                    return {"tool": "get_document", "args": {"doc_id": next_doc}}
                else:
                    findings = self._audit()
                    self._submitted = True
                    return {"tool": "submit", "args": {"findings": {"episode_id": self.episode_id, "findings": findings}}}

        # Если ошибка или неизвестное наблюдение
        findings = self._audit()
        self._submitted = True
        return {"tool": "submit", "args": {"findings": {"episode_id": self.episode_id, "findings": findings}}}

    def _find_duplicate_doc_ids(self) -> set[str]:
        doc_ids = set()
        by_key = {}
        for inv in self.invoices:
            k = (inv["entity_id"], inv["currency"], inv["total_minor"])
            by_key.setdefault(k, []).append(inv)
        for group in by_key.values():
            if len(group) > 1:
                for inv in group:
                    doc_ids.add(inv["doc_id"])
        return doc_ids

    def _audit(self) -> list[dict]:
        if not self.manifest:
            return []

        base = self.manifest["base_currency"]
        exponents = self.manifest["currency_exponents"]
        tz = _parse_tz(self.manifest["timezone"])

        if self.naive:
            # Наивная политика: использует UTC вместо локального пояса (ловушка late_evening_local)
            tz = timezone.utc

        period_start = datetime.fromisoformat(self.manifest["period_start"]).astimezone(tz)
        period_end = datetime.fromisoformat(self.manifest["period_end"]).astimezone(tz)

        def in_period(ts_str: str) -> bool:
            dt = datetime.fromisoformat(ts_str).astimezone(tz)
            return period_start <= dt < period_end

        def fx_factor(cur: str, ts_str: str) -> Decimal | None:
            """Множитель source -> base; None, если курса на эту дату нет в загруженных fx."""
            if cur == base:
                return Decimal(1)
            dt = datetime.fromisoformat(ts_str).astimezone(tz)
            date_str = dt.date().isoformat()
            fx_id = f"FX:{date_str}:{cur}:{base}"
            if fx_id not in self.fx:
                return None
            rate = Decimal(self.fx[fx_id]["rate"])
            diff = exponents[base] - exponents[cur]
            return rate.scaleb(diff)

        def convert(amount: int, cur: str, ts_str: str) -> int:
            factor = fx_factor(cur, ts_str)
            with localcontext() as ctx:
                ctx.prec = max(34, len(str(abs(amount))) + len(factor.as_tuple().digits) + 8)
                return int((Decimal(amount) * factor).to_integral_value(rounding=ROUND_HALF_EVEN))

        def source_amount(base_amount: int, cur: str, ts_str: str) -> int:
            factor = Fraction(fx_factor(cur, ts_str))
            lower = (Fraction(base_amount) - Fraction(1, 2)) / factor
            upper = (Fraction(base_amount) + Fraction(1, 2)) / factor
            low = max(0, -(-lower.numerator // lower.denominator))
            high = upper.numerator // upper.denominator
            if low <= high and convert(low, cur, ts_str) != base_amount:
                low += 1
            if high >= low and convert(high, cur, ts_str) != base_amount:
                high -= 1
            if low == high:
                return low
            return round(base_amount / float(fx_factor(cur, ts_str)))

        findings = []
        inv_by_id = {i["invoice_id"]: i for i in self.invoices}
        pay_by_id = {p["payment_id"]: p for p in self.payments}

        # Индекс проводок
        ledger_by_inv = {}
        ledger_by_pay = {}
        for r in self.ledger:
            if r["invoice_id"]:
                ledger_by_inv.setdefault(r["invoice_id"], []).append(r)
            if r["payment_id"]:
                ledger_by_pay.setdefault(r["payment_id"], []).append(r)

        # 1. duplicate_invoice
        dup_by_key = {}
        for inv in self.invoices:
            k = (inv["entity_id"], inv["currency"], inv["total_minor"])
            dup_by_key.setdefault(k, []).append(inv)

        used_invs = set()
        for group in dup_by_key.values():
            if len(group) < 2:
                continue
            group_sorted = sorted(group, key=lambda x: x["issued_at"])
            for idx in range(len(group_sorted) - 1):
                left, right = group_sorted[idx], group_sorted[idx + 1]
                if left["invoice_id"] in used_invs or right["invoice_id"] in used_invs:
                    continue
                d_left = datetime.fromisoformat(left["issued_at"]).astimezone(tz).date()
                d_right = datetime.fromisoformat(right["issued_at"]).astimezone(tz).date()
                gap = abs((d_left - d_right).days)
                if gap > 5 and not self.naive:
                    continue

                # Проверяем, что оба счёта проведены в книге (счёт 1200 > 0, 4000 < 0)
                l_rows = [r for r in ledger_by_inv.get(left["invoice_id"], []) if r["payment_id"] is None]
                r_rows = [r for r in ledger_by_inv.get(right["invoice_id"], []) if r["payment_id"] is None]
                if not (l_rows and r_rows):
                    continue

                # Проверка описания строк (для исключения legit_recurring)
                if not self.naive and left["doc_id"] in self.line_items_by_doc_id and right["doc_id"] in self.line_items_by_doc_id:
                    items_l = self.line_items_by_doc_id[left["doc_id"]]
                    items_r = self.line_items_by_doc_id[right["doc_id"]]
                    if items_l != items_r:
                        continue

                used_invs.add(left["invoice_id"])
                used_invs.add(right["invoice_id"])
                findings.append({
                    "type": "duplicate_invoice",
                    "affected_ids": sorted([left["invoice_id"], right["invoice_id"]]),
                    "claimed_amount_minor": right["total_minor"],
                    "currency": right["currency"],
                    "evidence_ids": sorted([left["doc_id"], right["doc_id"]]),
                    "explanation": f"Дубликат счёта: {right['invoice_id']} совпадает с {left['invoice_id']} (разница {gap} дн.)",
                })

        # 2. period_cutoff
        # Проверяем счета
        for inv in self.invoices:
            doc_in = in_period(inv["issued_at"])
            rows = [r for r in ledger_by_inv.get(inv["invoice_id"], []) if r["payment_id"] is None]
            if not doc_in and rows and any(in_period(r["timestamp"]) for r in rows):
                matching_row = next(r for r in rows if in_period(r["timestamp"]))
                findings.append({
                    "type": "period_cutoff",
                    "affected_ids": [inv["invoice_id"]],
                    "claimed_amount_minor": inv["total_minor"],
                    "currency": inv["currency"],
                    "evidence_ids": sorted([inv["doc_id"], matching_row["row_id"]]),
                    "explanation": f"Счёт {inv['invoice_id']} вне периода, но проведён в книге в отчётном периоде",
                })

        # Проверяем платежи
        for pay in self.payments:
            doc_in = in_period(pay["timestamp"])
            rows = ledger_by_pay.get(pay["payment_id"], [])
            if not doc_in and rows and any(in_period(r["timestamp"]) for r in rows):
                matching_row = next(r for r in rows if in_period(r["timestamp"]))
                findings.append({
                    "type": "period_cutoff",
                    "affected_ids": [pay["payment_id"]],
                    "claimed_amount_minor": abs(pay["amount_minor"]),
                    "currency": pay["currency"],
                    "evidence_ids": sorted([pay["doc_id"], matching_row["row_id"]]),
                    "explanation": f"Платёж {pay['payment_id']} вне периода, но проведён в книге в отчётном периоде",
                })

        # 3. payment_allocation
        for pay in self.payments:
            if pay["amount_minor"] <= 0:
                continue
            refs = pay.get("invoice_refs", [])
            if not refs:
                continue
            rows = ledger_by_pay.get(pay["payment_id"], [])
            if not rows:
                continue

            for ref in refs:
                inv_id = ref["invoice_id"]
                declared = ref["amount_minor"]
                inv = inv_by_id.get(inv_id)
                if not inv:
                    continue

                if fx_factor(pay["currency"], inv["issued_at"]) is None:
                    continue  # без курса сумму в валюте платежа не восстановить: не гадаем
                posted_1200_credit = sum(r["credit_minor"] for r in rows if r["account"] == "1200" and r["invoice_id"] == inv_id)
                has_2900 = any(r["account"] == "2900" and r["credit_minor"] > 0 for r in rows)
                allocated_in_pay_curr = source_amount(posted_1200_credit, pay["currency"], inv["issued_at"])
                unallocated = declared - allocated_in_pay_curr

                should_flag = (unallocated > 0) if self.naive else (unallocated > 0 and has_2900)
                if should_flag:
                    evidence = [pay["doc_id"]] + [r["row_id"] for r in rows]
                    findings.append({
                        "type": "payment_allocation",
                        "affected_ids": sorted([inv_id, pay["payment_id"]]),
                        "claimed_amount_minor": unallocated,
                        "currency": pay["currency"],
                        "evidence_ids": sorted(evidence),
                        "explanation": f"Платёж {pay['payment_id']} не полностью распределён на счёт {inv_id} (недораспределено {unallocated})",
                    })

        # 4. fx_conversion
        # Проверяем валютные счета
        for inv in self.invoices:
            if inv["currency"] == base:
                continue
            dt = datetime.fromisoformat(inv["issued_at"]).astimezone(tz)
            date_str = dt.date().isoformat()
            fx_id = f"FX:{date_str}:{inv['currency']}:{base}"
            if fx_id not in self.fx:
                continue
            expected_kzt = convert(inv["total_minor"], inv["currency"], inv["issued_at"])
            rows = [r for r in ledger_by_inv.get(inv["invoice_id"], []) if r["payment_id"] is None]
            key_row = next((r for r in rows if r["account"] == "1200" and r["debit_minor"] > 0), None)
            if key_row:
                posted_kzt = key_row["debit_minor"]
                diff = abs(posted_kzt - expected_kzt)
                if diff > 1:
                    findings.append({
                        "type": "fx_conversion",
                        "affected_ids": [inv["invoice_id"]],
                        "claimed_amount_minor": diff,
                        "currency": base,
                        "evidence_ids": sorted([inv["doc_id"], key_row["row_id"], fx_id]),
                        "explanation": f"Счёт {inv['invoice_id']} конвертирован с ошибкой курса (разница {diff} {base})",
                    })

        # Проверяем валютные платежи
        for pay in self.payments:
            if pay["currency"] == base or pay["amount_minor"] <= 0:
                continue
            dt = datetime.fromisoformat(pay["timestamp"]).astimezone(tz)
            date_str = dt.date().isoformat()
            fx_id = f"FX:{date_str}:{pay['currency']}:{base}"
            if fx_id not in self.fx:
                continue
            expected_kzt = convert(pay["amount_minor"], pay["currency"], pay["timestamp"])
            rows = ledger_by_pay.get(pay["payment_id"], [])
            key_row = next((r for r in rows if r["account"] == "1010" and r["debit_minor"] > 0), None)
            if key_row:
                posted_kzt = key_row["debit_minor"]
                diff = abs(posted_kzt - expected_kzt)
                if diff > 1:
                    findings.append({
                        "type": "fx_conversion",
                        "affected_ids": [pay["payment_id"]],
                        "claimed_amount_minor": diff,
                        "currency": base,
                        "evidence_ids": sorted([pay["doc_id"], key_row["row_id"], fx_id]),
                        "explanation": f"Платёж {pay['payment_id']} конвертирован с ошибкой курса (разница {diff} {base})",
                    })

        return _limit_findings(findings)


class SimpleRulePolicy(RuleBasedPolicy):
    """Простая сверка (Naive Auditor).

    Поверхностный алгоритм бухгалтерской сверки:
    - Не производит углублённого запроса первичных документов (get_document);
    - Использует время UTC вместо местного пояса (+05:00), попадая в ловушку late_evening_local;
    - Не анализирует позиции товаров и интервал дат, путая legit_recurring с дублями;
    - Игнорирует транзитный счёт 2900, ошибочно маркируя законный split_payment как ошибку.
    """

    def __init__(self):
        super().__init__(naive=True)
