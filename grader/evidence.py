"""Чтение открытых записей и независимая арифметика сертификата.

Модуль не импортирует generator, matcher или reward и не читает private/.
"""
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from fractions import Fraction
import json
from pathlib import Path

from common.jsonio import read_jsonl


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def read_public(directory: Path) -> dict:
    public = {}
    for name in ("manifest", "policy"):
        public[name] = json.loads((directory / f"{name}.json").read_text(encoding="utf-8"))
    for name in ("invoices", "payments"):
        public[name] = read_jsonl(directory / f"{name}.jsonl")
    for name in ("ledger", "fx"):
        with (directory / f"{name}.csv").open(encoding="utf-8", newline="") as stream:
            public[name] = list(csv.DictReader(stream))
    for row in public["ledger"]:
        for key in ("debit_minor", "credit_minor"):
            row[key] = int(row[key])
        for key in ("entity_id", "invoice_id", "payment_id"):
            if row[key] == public["manifest"]["csv_null"]:
                row[key] = None
    return public


class PublicEvidence:
    """Индексы открытых записей; деньги считаются без float."""

    def __init__(self, public: dict):
        self.manifest = public["manifest"]
        self.policy = public["policy"]
        self.base = self.manifest["base_currency"]
        self.exponents = self.manifest["currency_exponents"]
        offset = self.manifest["timezone"]
        minutes = int(offset[1:3]) * 60 + int(offset[4:6])
        self.tz = timezone(timedelta(minutes=minutes if offset[0] == "+" else -minutes))
        self.start = self.local(self.manifest["period_start"])
        self.end = self.local(self.manifest["period_end"])
        require(self.start < self.end, "invalid reporting period")
        require(self.policy["period"] == {
            "timezone": offset, "start_inclusive": True, "end_exclusive": True,
        }, "unsupported period policy")
        require(self.policy["fx"] == {
            "rate_date": "document_local_date", "direction": "1 source_currency = rate target_currency",
            "target_currency": self.base,
        }, "unsupported FX policy")
        require(self.policy["rounding"] == {
            "mode": "ROUND_HALF_EVEN", "apply": "after_conversion_to_target_exponent",
        } and self.manifest["rounding"]["mode"] == "ROUND_HALF_EVEN", "unsupported rounding policy")
        require(self.base in self.exponents and all(
            type(value) is int and 0 <= value <= 4 for value in self.exponents.values()
        ), "invalid currency exponents")
        self.records = {}
        self.invoices = self._index(public["invoices"], "invoice_id")
        self.payments = self._index(public["payments"], "payment_id")
        self.ledger = self._index(public["ledger"], "row_id")
        self.documents = self._index([*public["invoices"], *public["payments"]], "doc_id")
        self.fx = {}
        for row in public["fx"]:
            key = f"FX:{row['date']}:{row['source_currency']}:{row['target_currency']}"
            require(key not in self.records, "duplicate FX ID")
            require(type(row["rate"]) is str, "FX rate must be a decimal string")
            rate = Decimal(row["rate"])
            require(rate.is_finite() and rate > 0, "FX rate must be positive and finite")
            self.fx[key] = rate
            self.records[key] = row
        for doc in self.documents.values():
            self.local(self.timestamp(doc))
            require(doc["currency"] in self.exponents, "missing currency exponent")
            require(type(self.amount(doc)) is int, "document amount must be an integer")
            if "invoice_id" in doc:
                require(doc["total_minor"] >= 0 and bool(doc["line_items"]), "invalid invoice total/items")
                for line in doc["line_items"]:
                    require(all(type(line[key]) is int for key in
                                ("quantity", "unit_price_minor", "amount_minor")), "invalid line item amount")
                    require(line["quantity"] > 0 and line["unit_price_minor"] >= 0 and
                            line["quantity"] * line["unit_price_minor"] == line["amount_minor"],
                            "inconsistent invoice line item")
                require(sum(line["amount_minor"] for line in doc["line_items"]) == doc["total_minor"],
                        "invoice line items do not sum to total")
            else:
                for ref in doc["invoice_refs"]:
                    require(type(ref["amount_minor"]) is int and ref["amount_minor"] >= 0,
                            "invalid allocation amount")
                    invoice = self.invoices[ref["invoice_id"]]
                    require(invoice["currency"] == doc["currency"], "cross-currency allocation")
                require(sum(ref["amount_minor"] for ref in doc["invoice_refs"]) <= abs(doc["amount_minor"]),
                        "allocations exceed payment")
        for row in self.ledger.values():
            debit, credit = row["debit_minor"], row["credit_minor"]
            require(type(debit) is int and type(credit) is int and
                    debit >= 0 and credit >= 0 and (debit > 0) != (credit > 0), "invalid ledger amounts")
            require(row["currency"] == self.base, "ledger currency is not base currency")
            require(row["account"] in ("1010", "1200", "2900", "4000", "7900"), "unknown account")
            require(row["invoice_id"] is None or row["invoice_id"] in self.invoices, "unknown invoice link")
            require(row["payment_id"] is None or row["payment_id"] in self.payments, "unknown payment link")
            require(row["invoice_id"] is not None or row["payment_id"] is not None, "unlinked ledger row")
            self.local(row["timestamp"])

    def _index(self, rows: list[dict], key: str) -> dict:
        result = {}
        for row in rows:
            identifier = row[key]
            require(identifier not in self.records, "duplicate public record ID")
            result[identifier] = row
            self.records[identifier] = row
        return result

    def local(self, timestamp: str) -> datetime:
        value = datetime.fromisoformat(timestamp)
        require(value.tzinfo is not None, "timestamp lacks timezone")
        return value.astimezone(self.tz)

    def in_period(self, timestamp: str) -> bool:
        return self.start <= self.local(timestamp) < self.end

    @staticmethod
    def timestamp(doc: dict) -> str:
        return doc["issued_at"] if "invoice_id" in doc else doc["timestamp"]

    @staticmethod
    def amount(doc: dict) -> int:
        return doc["total_minor"] if "invoice_id" in doc else doc["amount_minor"]

    def fx_id(self, currency: str, timestamp: str) -> str:
        date = self.local(timestamp).date().isoformat()
        return f"FX:{date}:{currency}:{self.base}"

    def factor(self, currency: str, timestamp: str) -> Decimal:
        rate = Decimal(1) if currency == self.base else self.fx[self.fx_id(currency, timestamp)]
        with localcontext() as ctx:
            ctx.prec = max(34, len(rate.as_tuple().digits) + 8)
            return rate.scaleb(self.exponents[self.base] - self.exponents[currency])

    def convert(self, amount: int, currency: str, timestamp: str) -> int:
        factor = self.factor(currency, timestamp)
        with localcontext() as ctx:
            ctx.prec = max(34, len(str(abs(amount))) + len(factor.as_tuple().digits) + 8)
            return int((Decimal(amount) * factor).to_integral_value(rounding=ROUND_HALF_EVEN))

    def source_amount_bounds(self, base_amount: int, currency: str, timestamp: str) -> tuple[int, int]:
        """Exact integer preimage of ROUND_HALF_EVEN, including/excluding tie endpoints."""
        require(base_amount >= 0, "negative posted allocation")
        factor = Fraction(self.factor(currency, timestamp))
        lower = (Fraction(base_amount) - Fraction(1, 2)) / factor
        upper = (Fraction(base_amount) + Fraction(1, 2)) / factor
        low = max(0, -(-lower.numerator // lower.denominator))
        high = upper.numerator // upper.denominator
        if low <= high and self.convert(low, currency, timestamp) != base_amount:
            low += 1
        if high >= low and self.convert(high, currency, timestamp) != base_amount:
            high -= 1
        require(low <= high, "posted amount has no source amount under the rounding policy")
        return low, high

    def source_amount(self, base_amount: int, currency: str, timestamp: str) -> int:
        """Восстанавливает сумму только при однозначной обратной конвертации."""
        low, high = self.source_amount_bounds(base_amount, currency, timestamp)
        require(low == high, "allocation cannot be uniquely recovered after rounding")
        return low

    def recover_allocations(self, payment: dict) -> dict[str, int]:
        """Recover allocations jointly from receivables and the unallocated remainder.

        Every receivable constrains one integer amount, while account 2900
        constrains their sum at the payment-date rate. Intersect these intervals
        without enumerating their values; another date/rate can make a value
        unique even when its receivable posting alone cannot do so.
        """
        require(payment["amount_minor"] > 0, "allocation issue requires an incoming payment")
        declared, book = self.allocations(payment), self.book(payment)
        bounds = {}
        for identifier, amount in declared.items():
            low, high = self.source_amount_bounds(-book.get(("1200", identifier), 0),
                                                  payment["currency"], self.invoices[identifier]["issued_at"])
            low, high = max(0, low), min(amount, high)
            require(low <= high, "posted allocation is outside its declared range")
            bounds[identifier] = (low, high)
        remainder_low, remainder_high = self.source_amount_bounds(
            -book.get(("2900", None), 0), payment["currency"], payment["timestamp"])
        remainder_low = max(0, remainder_low)
        remainder_high = min(payment["amount_minor"], remainder_high)
        require(remainder_low <= remainder_high, "unallocated remainder is outside the payment amount")
        total_low = sum(low for low, _ in bounds.values())
        total_high = sum(high for _, high in bounds.values())
        sum_low = max(total_low, payment["amount_minor"] - remainder_high)
        sum_high = min(total_high, payment["amount_minor"] - remainder_low)
        require(sum_low <= sum_high, "receivables and unallocated remainder are inconsistent")
        allocated = {}
        for identifier, (low, high) in bounds.items():
            feasible_low = max(low, sum_low - (total_high - high))
            feasible_high = min(high, sum_high - (total_low - low))
            require(feasible_low == feasible_high, "allocation cannot be uniquely recovered after rounding")
            allocated[identifier] = feasible_low
        require(self.payment_book(payment, allocated) == book,
                "unallocated remainder or FX postings are inconsistent")
        return allocated

    def rows(self, doc: dict) -> list[dict]:
        if "payment_id" in doc:
            return [row for row in self.ledger.values() if row["payment_id"] == doc["payment_id"]]
        return [row for row in self.ledger.values()
                if row["invoice_id"] == doc["invoice_id"] and row["payment_id"] is None]

    @staticmethod
    def balance(rows: list[dict]) -> int:
        return sum(row["debit_minor"] - row["credit_minor"] for row in rows)

    def book(self, doc: dict) -> dict:
        totals = {}
        for row in self.rows(doc):
            invoice_id = row["invoice_id"] if row["account"] == "1200" else None
            key = (row["account"], invoice_id)
            totals[key] = totals.get(key, 0) + row["debit_minor"] - row["credit_minor"]
        return {key: value for key, value in totals.items() if value}

    def payment_book(self, doc: dict, allocations: dict[str, int]) -> dict:
        """Ожидаемые проводки по открытой выписке и фактическим распределениям."""
        sign = 1 if doc["amount_minor"] >= 0 else -1
        expected = {("1010", None): self.convert(doc["amount_minor"], doc["currency"], doc["timestamp"])}
        for invoice_id, amount in sorted(allocations.items()):
            invoice = self.invoices[invoice_id]
            expected[("1200", invoice_id)] = -sign * self.convert(amount, doc["currency"], invoice["issued_at"])
        remainder = abs(doc["amount_minor"]) - sum(allocations.values())
        require(remainder >= 0, "posted allocations exceed payment")
        expected[("2900", None)] = -sign * self.convert(remainder, doc["currency"], doc["timestamp"])
        expected[("7900", None)] = -sum(expected.values())
        return {key: value for key, value in expected.items() if value}

    @staticmethod
    def allocations(doc: dict) -> dict[str, int]:
        result = {}
        for ref in doc["invoice_refs"]:
            result[ref["invoice_id"]] = result.get(ref["invoice_id"], 0) + ref["amount_minor"]
        return result

    def assert_clean(self, doc: dict) -> None:
        if "invoice_id" in doc:
            value = self.convert(doc["total_minor"], doc["currency"], doc["issued_at"])
            expected = {("1200", doc["invoice_id"]): value, ("4000", None): -value}
            expected = {key: value for key, value in expected.items() if value}
        else:
            expected = self.payment_book(doc, self.allocations(doc))
        require(self.book(doc) == expected, "document postings do not follow public policy")
        require(all(self.in_period(row["timestamp"]) == self.in_period(self.timestamp(doc))
                    for row in self.rows(doc)), "document posted in wrong period")
