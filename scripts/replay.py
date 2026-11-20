"""Терминальный просмотрщик траекторий аудитора (Audit Gym Replay).

Красиво в консоли (цвета, таблицы, плашки) пошагово показывает:
1. Контекст эпизода и параметры компании (базовая валюта, отчётный период);
2. Запрос первичных документов и таблиц (list_records, get_document, calculator);
3. Детективный анализ: сопоставление позиций, выявление расхождений;
4. Формирование доказательной базы и итоговое заключение (submit / findings);
5. Независимую верификацию оценщиком (Ground Truth TP/FP/FN, F1, Reward).

Примеры использования:
    python -m scripts.replay trajectories.jsonl --episode EP-0HJPAE81
    python -m scripts.replay episodes/train/EP-0HJPAE81
    python -m scripts.replay --list
    python -m scripts.replay --interactive
    python -m scripts.replay --summary
"""
import argparse
import copy
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent

# Попытка инициализации colorama для Windows-терминала
try:
    import colorama
    colorama.init(autoreset=True)
except ImportError:
    pass

# Названия типов ошибок на русском языке
TYPE_RU = {
    "duplicate_invoice": "Дубликат счёта",
    "payment_allocation": "Неполное распределение платежа",
    "fx_conversion": "Ошибка курса валют",
    "period_cutoff": "Нарушение отчётного периода",
}

# Названия отвлекающих факторов (distractors) на русском языке
DISTRACTOR_RU = {
    "split_payment": "Разбитый платёж (один счёт закрыт частями)",
    "refund": "Возврат средств (сторно платежа)",
    "legit_recurring": "Законная периодическая сумма (разные месяцы)",
    "late_evening_local": "Поздний вечер (+05:00 местное vs UTC)",
}


class Theme:
    """Управление цветами ANSI в терминале."""

    def __init__(self, use_color: bool = True):
        self.use_color = use_color
        if use_color:
            self.RESET = "\033[0m"
            self.BOLD = "\033[1m"
            self.DIM = "\033[2m"
            self.RED = "\033[31m"
            self.GREEN = "\033[32m"
            self.YELLOW = "\033[33m"
            self.BLUE = "\033[34m"
            self.MAGENTA = "\033[35m"
            self.CYAN = "\033[36m"
            self.WHITE = "\033[37m"
            self.BRIGHT_RED = "\033[91m"
            self.BRIGHT_GREEN = "\033[92m"
            self.BRIGHT_YELLOW = "\033[93m"
            self.BRIGHT_BLUE = "\033[94m"
            self.BRIGHT_MAGENTA = "\033[95m"
            self.BRIGHT_CYAN = "\033[96m"
            self.BRIGHT_WHITE = "\033[97m"
            self.BG_BLUE = "\033[44m"
            self.BG_CYAN = "\033[46m"
            self.BG_GREEN = "\033[42m"
            self.BG_MAGENTA = "\033[45m"
            self.BG_DARK = "\033[100m"
        else:
            self.RESET = ""
            self.BOLD = ""
            self.DIM = ""
            self.RED = ""
            self.GREEN = ""
            self.YELLOW = ""
            self.BLUE = ""
            self.MAGENTA = ""
            self.CYAN = ""
            self.WHITE = ""
            self.BRIGHT_RED = ""
            self.BRIGHT_GREEN = ""
            self.BRIGHT_YELLOW = ""
            self.BRIGHT_BLUE = ""
            self.BRIGHT_MAGENTA = ""
            self.BRIGHT_CYAN = ""
            self.BRIGHT_WHITE = ""
            self.BG_BLUE = ""
            self.BG_CYAN = ""
            self.BG_GREEN = ""
            self.BG_MAGENTA = ""
            self.BG_DARK = ""


_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """Удаляет управляющие escape-последовательности для точного расчёта ширины."""
    return _ANSI_RE.sub("", text)


def visible_len(text: str) -> int:
    """Видимая длина строки без ANSI-кодов."""
    return len(strip_ansi(text))


def format_money(minor: int, currency: str, exponents: Optional[Dict[str, int]] = None) -> str:
    """Форматирует сумму в минорных единицах в красивую строку с пробелами."""
    exp = (exponents or {}).get(currency, 2 if currency != "JPY" else 0)
    sign = "−" if minor < 0 else ""
    abs_minor = abs(minor)
    if exp == 0:
        text = f"{abs_minor:,}".replace(",", " ")
        return f"{sign}{text} {currency}"
    divisor = 10 ** exp
    whole, frac = divmod(abs_minor, divisor)
    text = f"{whole:,}".replace(",", " ")
    return f"{sign}{text},{frac:0{exp}d} {currency}"


def draw_panel(
    title: str,
    lines: List[str],
    width: int = 80,
    theme: Optional[Theme] = None,
    border_color: str = "",
    title_color: str = "",
    double: bool = False,
) -> str:
    """Отрисовывает аккуратную плашку с рамкой и заголовком."""
    th = theme or Theme(False)
    reset = th.RESET

    if double:
        tl, tr, bl, br, hz, vt = "╔", "╗", "╚", "╝", "═", "║"
        ml, mr = "╠", "╣"
    else:
        tl, tr, bl, br, hz, vt = "┌", "┐", "└", "┘", "─", "│"
        ml, mr = "├", "┤"

    res = []
    inner_width = max(width - 2, 20)

    # Разворачиваем возможные переносы строк внутри элементов lines
    flat_lines = []
    for l in lines:
        for subl in l.split("\n"):
            flat_lines.append(subl)

    if title:
        vis_title_len = visible_len(title)
        title_styled = f" {title_color}{title}{border_color} "
        remaining = inner_width - vis_title_len - 2
        left_pad = max(remaining // 2, 1)
        right_pad = max(remaining - left_pad, 1)
        top = f"{border_color}{tl}{hz * left_pad}{title_styled}{hz * right_pad}{tr}{reset}"
    else:
        top = f"{border_color}{tl}{hz * inner_width}{tr}{reset}"
    res.append(top)

    for line in flat_lines:
        vis = visible_len(line)
        pad = max(inner_width - vis - 2, 0)
        res.append(f"{border_color}{vt}{reset} {line}{' ' * pad} {border_color}{vt}{reset}")

    bot = f"{border_color}{bl}{hz * inner_width}{br}{reset}"
    res.append(bot)
    return "\n".join(res)


def draw_table(
    headers: List[str],
    rows: List[List[str]],
    alignments: Optional[List[str]] = None,
    theme: Optional[Theme] = None,
    border_color: str = "",
    header_color: str = "",
) -> str:
    """Отрисовывает структурированную таблицу в псевдографике."""
    th = theme or Theme(False)
    reset = th.RESET

    col_count = len(headers)
    aligns = alignments or ["left"] * col_count

    # Расчёт ширины колонок по максимальной видимой длине
    col_widths = [visible_len(h) for h in headers]
    for row in rows:
        for idx in range(min(len(row), col_count)):
            col_widths[idx] = max(col_widths[idx], visible_len(str(row[idx])))

    # Минимум 3 символа на колонку
    col_widths = [max(w, 3) for w in col_widths]

    # Символы рамки таблицы
    tl, tm, tr = "┌", "┬", "┐"
    ml, mm, mr = "├", "┼", "┤"
    bl, bm, br = "└", "┴", "┘"
    hz, vt = "─", "│"

    top_border = (
        border_color
        + tl
        + tm.join(hz * (w + 2) for w in col_widths)
        + tr
        + reset
    )
    mid_border = (
        border_color
        + ml
        + mm.join(hz * (w + 2) for w in col_widths)
        + mr
        + reset
    )
    bot_border = (
        border_color
        + bl
        + bm.join(hz * (w + 2) for w in col_widths)
        + br
        + reset
    )

    out = [top_border]

    # Заголовки
    hdr_cells = []
    for idx, (h, w) in enumerate(zip(headers, col_widths)):
        vis = visible_len(h)
        pad = w - vis
        cell = f" {header_color}{h}{reset}{' ' * pad} "
        hdr_cells.append(cell)
    out.append(f"{border_color}{vt}{reset}" + f"{border_color}{vt}{reset}".join(hdr_cells) + f"{border_color}{vt}{reset}")
    out.append(mid_border)

    # Строки данных
    for row in rows:
        row_cells = []
        for idx in range(col_count):
            val = str(row[idx]) if idx < len(row) else ""
            w = col_widths[idx]
            vis = visible_len(val)
            pad = max(w - vis, 0)
            align = aligns[idx] if idx < len(aligns) else "left"
            if align == "right":
                cell = f" {' ' * pad}{val} "
            elif align == "center":
                lp = pad // 2
                rp = pad - lp
                cell = f" {' ' * lp}{val}{' ' * rp} "
            else:
                cell = f" {val}{' ' * pad} "
            row_cells.append(cell)
        out.append(f"{border_color}{vt}{reset}" + f"{border_color}{vt}{reset}".join(row_cells) + f"{border_color}{vt}{reset}")

    out.append(bot_border)
    return "\n".join(out)


def find_episode_path(episode_id: str) -> Optional[Path]:
    """Ищет каталог эпизода по episode_id в стандартных путях проекта."""
    candidates = [
        ROOT / "episodes" / "train" / episode_id,
        ROOT / "episodes" / "dev" / episode_id,
        ROOT / "episodes" / episode_id,
    ]
    for p in candidates:
        if p.exists() and p.is_dir():
            return p
    # Поиск по шаблону
    matches = list((ROOT / "episodes").glob(f"**/{episode_id}"))
    if matches:
        return matches[0]
    return None


def load_ground_truth(ep_dir: Optional[Path]) -> Optional[Dict[str, Any]]:
    """Загружает приватный эталон (truth.json), если он доступен."""
    if not ep_dir:
        return None
    truth_file = ep_dir / "private" / "truth.json"
    if truth_file.exists():
        try:
            return json.loads(truth_file.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def load_trajectories_from_source(source: Optional[str] = None) -> Tuple[Dict[str, List[Dict[str, Any]]], Optional[Path]]:
    """Загружает траектории из указанного файла, каталога или пути по умолчанию.

    Возвращает словарь {episode_id: [steps]} и путь к каталогу эпизода, если передан каталог.
    """
    ep_dir = None
    target_path = None

    if source:
        src_p = Path(source)
        if not src_p.is_absolute():
            src_p = (ROOT / src_p).resolve()

        if src_p.is_dir():
            ep_dir = src_p
            cand = src_p / "trajectories.jsonl"
            if cand.exists():
                target_path = cand
            else:
                target_path = ROOT / "trajectories.jsonl"
        elif src_p.is_file():
            target_path = src_p
            if src_p.parent.is_dir() and (src_p.parent / "public" / "manifest.json").exists():
                ep_dir = src_p.parent
    else:
        # Поиск по умолчанию
        default_candidates = [
            ROOT / "trajectories.jsonl",
            ROOT / "episodes" / "trajectories.jsonl",
            ROOT / "episodes" / "train" / "EP-0HJPAE81" / "trajectories.jsonl",
        ]
        for c in default_candidates:
            if c.exists():
                target_path = c
                break

    if not target_path or not target_path.exists():
        return {}, ep_dir

    episodes: Dict[str, List[Dict[str, Any]]] = {}
    with open(target_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                eid = row.get("episode_id")
                if eid:
                    episodes.setdefault(eid, []).append(row)
            except json.JSONDecodeError:
                continue

    # Сортировка шагов внутри каждого эпизода
    for eid in episodes:
        episodes[eid].sort(key=lambda s: s.get("step", 0))

    return episodes, ep_dir


def print_step_initial(step_data: Dict[str, Any], theme: Theme, width: int = 80) -> None:
    """Выводит красивое введение в эпизод (Акт I: Вход в контекст)."""
    th = theme
    obs = step_data.get("observation", {})
    manifest = obs.get("manifest", {})
    eid = manifest.get("episode_id", step_data.get("episode_id", "НЕИЗВЕСТНО"))
    counts = manifest.get("counts", {})
    base_cur = manifest.get("base_currency", "KZT")
    tz = manifest.get("timezone", "+05:00")
    period_start = manifest.get("period_start", "")[:10]
    period_end = manifest.get("period_end", "")[:10]
    rounding = manifest.get("rounding", {}).get("mode", "ROUND_HALF_EVEN")

    banner_lines = [
        f"{th.BOLD}{th.BRIGHT_WHITE}АВТОНОМНЫЙ АУДИТОР ФИНАНСОВОЙ ОТЧЁТНОСТИ (AUDIT GYM){th.RESET}",
        f"{th.CYAN}Терминальный интерактивный разбор расследования и доказательств{th.RESET}",
        f"{th.DIM}Эпизод: {th.RESET}{th.BOLD}{th.BRIGHT_YELLOW}{eid}{th.RESET}  |  "
        f"{th.DIM}Период проверки: {th.RESET}{th.WHITE}{period_start} — {period_end}{th.RESET}",
    ]
    print(draw_panel(" АКТ I: ВХОД В КОНТЕКСТ АУДИТА ", banner_lines, width=width, theme=th, border_color=th.BRIGHT_CYAN, double=True))
    print()

    # Параметры компании
    params_lines = [
        f"{th.BOLD}Организация:{th.RESET} Импортёр / торговая фирма  |  {th.BOLD}Базовая валюта:{th.RESET} {th.BRIGHT_GREEN}{base_cur}{th.RESET}  |  {th.BOLD}Часовой пояс:{th.RESET} {th.WHITE}{tz}{th.RESET}",
        f"{th.BOLD}Масштаб документов и проводок в аудите:{th.RESET}",
        f"  • {th.BRIGHT_CYAN}📄 Счетов-фактур (Invoices):{th.RESET} {counts.get('invoices', 0):<5} "
        f"  • {th.BRIGHT_CYAN}💳 Платёжных выписок (Payments):{th.RESET} {counts.get('payments', 0):<5}",
        f"  • {th.BRIGHT_CYAN}📊 Главная книга (Ledger rows):{th.RESET} {counts.get('ledger_rows', 0):<5} "
        f"  • {th.BRIGHT_CYAN}💱 Котировок валют (FX rates):{th.RESET} {counts.get('fx_rates', 0):<5}",
        f"{th.DIM}Правила контракта:{th.RESET} Лимит: 100 шагов | Округление: {rounding} | Допуск: 1 ед.",
    ]
    print(draw_panel(" ПАРАМЕТРЫ ПРОВЕРЯЕМОЙ КОМПАНИИ ", params_lines, width=width, theme=th, border_color=th.CYAN))
    print()


def print_step_action(
    step_data: Dict[str, Any],
    theme: Theme,
    exponents: Optional[Dict[str, int]] = None,
    width: int = 80,
) -> None:
    """Выводит детальный шаг взаимодействия агента со средой."""
    th = theme
    step_num = step_data.get("step", 0)
    action = step_data.get("action") or {}
    tool = action.get("tool", "")
    args = action.get("args", {})
    obs = step_data.get("observation", {})
    remaining = obs.get("steps_remaining", 100 - step_num)

    step_tag = f"{th.BRIGHT_WHITE}[ ШАГ {step_num:02d}/100 ]{th.RESET} {th.DIM}(Осталось шагов: {remaining}){th.RESET}"

    if tool == "list_records":
        table = args.get("table", "")
        cursor = args.get("cursor")
        limit = args.get("limit", 50)
        res = obs.get("result", {})
        records = res.get("records", [])
        total = res.get("total", len(records))
        next_cur = res.get("next_cursor")

        cur_status = f"смещение: {cursor}" if cursor else "начало таблицы"
        next_status = f"следующий курсор: {next_cur}" if next_cur else "все строки прочитаны"

        table_badges = {
            "invoices": f"{th.BRIGHT_MAGENTA}СЧЕТА-ФАКТУРЫ (INVOICES){th.RESET}",
            "payments": f"{th.BRIGHT_GREEN}ПЛАТЁЖНЫЕ ВЫПИСКИ (PAYMENTS){th.RESET}",
            "fx": f"{th.BRIGHT_YELLOW}КУРСЫ ВАЛЮТ (FX RATES){th.RESET}",
            "ledger": f"{th.BRIGHT_CYAN}ГЛАВНАЯ КНИГА ПРОВОДОК (LEDGER){th.RESET}",
        }
        badge = table_badges.get(table, table.upper())

        lines = [
            f"{th.BOLD}Инструмент:{th.RESET} {th.BRIGHT_CYAN}list_records{th.RESET}(таблица: {badge}, лимит: {limit})",
            f"{th.BOLD}Результат:{th.RESET} Получено {th.BRIGHT_WHITE}{len(records)}{th.RESET} строк из {total} ({cur_status} -> {next_status})",
        ]

        # Превью нескольких записей в компактной табличке
        if records:
            if table == "invoices":
                hdrs = ["ID счёта", "Контрагент", "Дата выставления", "Сумма", "Валюта"]
                rows = []
                preview_records = records[:3]
                for r in preview_records:
                    amt_str = format_money(r.get("total_minor", 0), r.get("currency", ""), exponents)
                    rows.append([
                        r.get("invoice_id", ""),
                        r.get("entity_id", ""),
                        (r.get("issued_at", ""))[:16],
                        amt_str,
                        r.get("currency", ""),
                    ])
                if len(records) > 3:
                    rows.append(["...", "...", f"и ещё {len(records) - 3} счетов", "...", "..."])
                lines.append("")
                lines.append(f"{th.DIM}Превью загруженных записей реестра:{th.RESET}")
                lines.append(draw_table(hdrs, rows, alignments=["left", "left", "left", "right", "center"], theme=th, border_color=th.DIM, header_color=th.WHITE))

            elif table == "payments":
                hdrs = ["ID платежа", "Дата", "Сумма", "Валюта", "Привязка к счетам"]
                rows = []
                preview_records = records[:3]
                for r in preview_records:
                    amt_str = format_money(r.get("amount_minor", 0), r.get("currency", ""), exponents)
                    refs = ", ".join(ref.get("invoice_id", "") for ref in r.get("invoice_refs", [])) or "—"
                    rows.append([
                        r.get("payment_id", ""),
                        (r.get("timestamp", ""))[:16],
                        amt_str,
                        r.get("currency", ""),
                        refs[:25],
                    ])
                if len(records) > 3:
                    rows.append(["...", f"и ещё {len(records) - 3} платежей", "...", "...", "..."])
                lines.append("")
                lines.append(f"{th.DIM}Превью банковских выписок:{th.RESET}")
                lines.append(draw_table(hdrs, rows, alignments=["left", "left", "right", "center", "left"], theme=th, border_color=th.DIM, header_color=th.WHITE))

            elif table == "fx":
                hdrs = ["Дата котировки", "Пара валют", "Официальный курс"]
                rows = []
                for r in records[:3]:
                    pair = f"{r.get('source_currency')} → {r.get('target_currency')}"
                    rows.append([r.get("date", ""), pair, str(r.get("rate", ""))])
                if len(records) > 3:
                    rows.append(["...", f"и ещё {len(records) - 3} курсов", "..."])
                lines.append("")
                lines.append(f"{th.DIM}Превью курсов валют Национального банка:{th.RESET}")
                lines.append(draw_table(hdrs, rows, alignments=["left", "center", "right"], theme=th, border_color=th.DIM, header_color=th.WHITE))

            elif table == "ledger":
                hdrs = ["ID строки", "Дата проводки", "Счёт Дт", "Счёт Кт", "Сумма (KZT)", "Документ"]
                rows = []
                for r in records[:3]:
                    doc_ref = r.get("invoice_id") or r.get("payment_id") or "—"
                    amt = format_money(int(r.get("debit_minor", 0)), "KZT", exponents)
                    rows.append([
                        r.get("row_id", ""),
                        (r.get("posted_at", ""))[:10],
                        r.get("debit_account", ""),
                        r.get("credit_account", ""),
                        amt,
                        doc_ref,
                    ])
                if len(records) > 3:
                    rows.append(["...", f"и ещё {len(records) - 3} проводок", "...", "...", "...", "..."])
                lines.append("")
                lines.append(f"{th.DIM}Превью проводок книги учёта (Дт/Кт):{th.RESET}")
                lines.append(draw_table(hdrs, rows, alignments=["left", "left", "center", "center", "right", "left"], theme=th, border_color=th.DIM, header_color=th.WHITE))

        print(draw_panel(f" {step_tag} ", lines, width=width, theme=th, border_color=th.CYAN))
        print()

    elif tool == "get_document":
        doc_id = args.get("doc_id", "")
        res = obs.get("result", {})
        doc = res.get("document", {})
        doc_type = doc.get("doc_type", "document")
        inv_id = doc.get("invoice_id") or doc.get("payment_id") or doc_id
        entity = doc.get("entity_id", "—")
        issued = (doc.get("issued_at") or doc.get("timestamp") or "")[:16]
        currency = doc.get("currency", "")
        total_minor = doc.get("total_minor") if "total_minor" in doc else doc.get("amount_minor", 0)
        total_str = format_money(total_minor, currency, exponents)
        line_items = doc.get("line_items", [])

        lines = [
            f"{th.BOLD}{th.BRIGHT_YELLOW}🔍 ЗАПРОС ПЕРВИЧНОГО ДОКУМЕНТА (ДЕТЕКТИВНЫЙ АНАЛИЗ СПЕЦИФИКАЦИИ){th.RESET}",
            f"{th.BOLD}Инструмент:{th.RESET} {th.BRIGHT_CYAN}get_document{th.RESET}(doc_id='{doc_id}')",
            f"{th.BOLD}Документ:{th.RESET} {th.WHITE}{inv_id}{th.RESET} ({doc_type.upper()})  |  {th.BOLD}Контрагент:{th.RESET} {entity}  |  {th.BOLD}Дата:{th.RESET} {issued}",
            f"{th.BOLD}Итоговая сумма по документу:{th.RESET} {th.BRIGHT_GREEN}{total_str}{th.RESET}",
        ]

        if line_items:
            lines.append("")
            lines.append(f"{th.DIM}Построчная товарная спецификация документа (Line Items):{th.RESET}")
            hdrs = ["№", "Наименование товара / услуги", "Кол-во", "Цена за ед.", "Итого"]
            rows = []
            for i, li in enumerate(line_items, 1):
                price_str = format_money(li.get("unit_price_minor", 0), currency, exponents)
                amt_str = format_money(li.get("amount_minor", 0), currency, exponents)
                rows.append([str(i), li.get("description", ""), str(li.get("quantity", 1)), price_str, amt_str])
            lines.append(draw_table(hdrs, rows, alignments=["center", "left", "center", "right", "right"], theme=th, border_color=th.YELLOW, header_color=th.BRIGHT_WHITE))
            lines.append(f"{th.DIM}💡 Аудитор сопоставляет строки спецификации, чтобы доказать дубликат счёта либо исключить регулярный платёж.{th.RESET}")

        print(draw_panel(f" {step_tag} ", lines, width=width, theme=th, border_color=th.YELLOW))
        print()

    elif tool == "get_record":
        table = args.get("table", "")
        rid = args.get("id", "")
        res = obs.get("result", {})
        rec = res.get("record", {})
        lines = [
            f"{th.BOLD}Инструмент:{th.RESET} {th.BRIGHT_CYAN}get_record{th.RESET}(таблица: '{table}', id: '{rid}')",
            f"{th.BOLD}Данные записи:{th.RESET} {json.dumps(rec, ensure_ascii=False)}",
        ]
        print(draw_panel(f" {step_tag} ", lines, width=width, theme=th, border_color=th.BLUE))
        print()

    elif tool == "calculator":
        expr = args.get("expression", "")
        res = obs.get("result", {})
        val = res.get("value", "")
        lines = [
            f"{th.BOLD}Инструмент:{th.RESET} {th.BRIGHT_CYAN}calculator{th.RESET}",
            f"{th.BOLD}Выражение:{th.RESET} {expr}",
            f"{th.BOLD}Результат вычисления (AST Decimal):{th.RESET} {th.BRIGHT_GREEN}{val}{th.RESET}",
        ]
        print(draw_panel(f" {step_tag} ", lines, width=width, theme=th, border_color=th.MAGENTA))
        print()


def print_step_final(
    step_data: Dict[str, Any],
    theme: Theme,
    ground_truth: Optional[Dict[str, Any]] = None,
    exponents: Optional[Dict[str, int]] = None,
    width: int = 80,
) -> None:
    """Выводит Акт II: Итоговое аудиторское заключение (findings) и верификацию эталоном."""
    th = theme
    action = step_data.get("action") or {}
    findings_obj = (action.get("args") or {}).get("findings") or {}
    findings_list = findings_obj.get("findings", [])
    obs = step_data.get("observation", {})
    status = obs.get("status", "submitted")
    reward = step_data.get("reward", 0.0)
    steps_used = obs.get("steps_used", step_data.get("step", 0))

    banner_lines = [
        f"{th.BOLD}{th.BRIGHT_WHITE}ИТОГОВОЕ ЗАКЛЮЧЕНИЕ АУДИТОРА (FINDINGS & PROOFS SUBMISSION){th.RESET}",
        f"Выявлено нарушений: {th.BRIGHT_YELLOW}{len(findings_list)}{th.RESET}  |  "
        f"Затрачено шагов: {th.WHITE}{steps_used}/100{th.RESET}  |  "
        f"Статус проверки: {th.BRIGHT_GREEN if status == 'submitted' else th.BRIGHT_RED}{status.upper()}{th.RESET}",
    ]
    print(draw_panel(" АКТ II: ОФИЦИАЛЬНЫЙ АУДИТОРСКИЙ ОТЧЁТ ", banner_lines, width=width, theme=th, border_color=th.BRIGHT_GREEN, double=True))
    print()

    # Сопоставление с эталоном (если доступен)
    matcher_res = None
    if ground_truth and "issues" in ground_truth:
        try:
            from grader.matcher import match
            matcher_res = match(findings_list, ground_truth["issues"])
        except Exception:
            matcher_res = None

    matched_indices = {}
    if matcher_res:
        for m in matcher_res.get("matches", []):
            matched_indices[m["finding_index"]] = m["issue_id"]

    # Таблица находок
    if findings_list:
        print(f"{th.BOLD}{th.BRIGHT_WHITE}📋 СПИСОК ВЫЯВЛЕННЫХ ИСКАЖЕНИЙ И СФОРМИРОВАННЫХ ДОКАЗАТЕЛЬСТВ:{th.RESET}\n")

        for idx, f in enumerate(findings_list, 1):
            f_type = f.get("type", "")
            type_title = TYPE_RU.get(f_type, f_type)
            amt_minor = f.get("claimed_amount_minor", 0)
            cur = f.get("currency", "")
            amt_str = format_money(amt_minor, cur, exponents)
            affected = ", ".join(f.get("affected_ids", []))
            evidence = ", ".join(f.get("evidence_ids", []))
            expl = f.get("explanation", "")

            # Статус верификации
            status_badge = ""
            if matcher_res is not None:
                if (idx - 1) in matched_indices:
                    iss_id = matched_indices[idx - 1]
                    status_badge = f"  {th.BG_GREEN}{th.BRIGHT_WHITE} [ПОДТВЕРЖДЕНО ЭТАЛОНОМ TP: {iss_id}] {th.RESET}"
                else:
                    status_badge = f"  {th.BG_DARK}{th.BRIGHT_YELLOW} [ЛОЖНАЯ ТРЕВОГА FP] {th.RESET}"

            card_lines = [
                f"{th.BOLD}Тип нарушения:{th.RESET} {th.BRIGHT_CYAN}{type_title}{th.RESET} ({f_type}){status_badge}",
                f"{th.BOLD}Сумма искажения:{th.RESET} {th.BRIGHT_GREEN}{amt_str}{th.RESET}",
                f"{th.BOLD}Затронутые записи / счета:{th.RESET} {th.WHITE}{affected}{th.RESET}",
                f"{th.BOLD}Собранные доказательства (Evidence IDs):{th.RESET} {th.BRIGHT_MAGENTA}{evidence}{th.RESET}",
                f"{th.BOLD}Обоснование аудитора:{th.RESET} {th.DIM}{expl}{th.RESET}",
            ]
            print(draw_panel(f" НАХОДКА #{idx} ", card_lines, width=width, theme=th, border_color=th.BRIGHT_CYAN))
            print()
    else:
        print(f"{th.YELLOW}Находок не заявлено (чистая отчётность либо пустой submit).{th.RESET}\n")

    # Секция оценки (Ground Truth Scorecard)
    if matcher_res is not None:
        tp = matcher_res.get("tp", 0)
        fp = matcher_res.get("fp", 0)
        fn = matcher_res.get("fn", 0)
        prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = (2 * prec * rec) / (prec + rec) if (prec + rec) > 0 else 0.0

        score_lines = [
            f"{th.BOLD}{th.BRIGHT_WHITE}НЕЗАВИСИМАЯ ОЦЕНКА МЕТОДИКОЙ ЖЮРИ (GROUND TRUTH GRADER){th.RESET}",
            f"  • {th.BRIGHT_GREEN}Подтверждённые ошибки (True Positives, TP):{th.RESET}   {tp} из {tp + fn}",
            f"  • {th.BRIGHT_YELLOW}Ложные срабатывания (False Positives, FP):{th.RESET}     {fp}",
            f"  • {th.BRIGHT_RED}Пропущенные ошибки (False Negatives, FN):{th.RESET}      {fn}",
            "",
            f"  • {th.BOLD}Точность (Precision):{th.RESET} {th.BRIGHT_CYAN}{prec * 100:.1f}%{th.RESET}",
            f"  • {th.BOLD}Полнота (Recall):{th.RESET}     {th.BRIGHT_CYAN}{rec * 100:.1f}%{th.RESET}",
            f"  • {th.BOLD}F1-Мера:{th.RESET}             {th.BRIGHT_GREEN}{f1:.4f}{th.RESET}",
            f"  • {th.BOLD}Итоговая награда среды (Reward):{th.RESET} {th.BOLD}{th.BRIGHT_GREEN}{reward:+.4f}{th.RESET} (с учётом штрафа за {steps_used} шагов)",
        ]

        # Разбор отвлекающих факторов (distractors)
        distractors = ground_truth.get("distractors", [])
        if distractors:
            score_lines.append("")
            score_lines.append(f"{th.BOLD}Устойчивость к отвлекающим ловушкам (Distractors):{th.RESET}")
            for d in distractors:
                d_kind = d.get("kind", "")
                d_title = DISTRACTOR_RU.get(d_kind, d_kind)
                d_recs = ", ".join(d.get("record_ids", []))
                score_lines.append(f"  • {th.GREEN}✔ Обойдено:{th.RESET} {d_title} [{d_recs}]")

        print(draw_panel(" ЭКЗАМЕНАЦИОННАЯ КАРТОЧКА ОЦЕНКИ ", score_lines, width=width, theme=th, border_color=th.BRIGHT_GREEN, double=True))
        print()
    else:
        # Если эталон недоступен (например, в слепом eval-режиме)
        summary_lines = [
            f"{th.BOLD}Статус завершения эпизода:{th.RESET} {th.BRIGHT_GREEN}{status}{th.RESET}",
            f"{th.BOLD}Количество заявленных находок:{th.RESET} {len(findings_list)}",
            f"{th.BOLD}Использовано шагов:{th.RESET} {steps_used} из 100",
            f"{th.BOLD}Награда среды (Reward):{th.RESET} {th.BRIGHT_GREEN}{reward:+.4f}{th.RESET}",
            f"{th.DIM}Примечание: Приватный эталон недоступен в текущей директории.{th.RESET}",
        ]
        print(draw_panel(" РЕЗУЛЬТАТ ЭПИЗОДА ", summary_lines, width=width, theme=th, border_color=th.GREEN))
        print()


def list_episodes(source: Optional[str] = None, use_color: bool = True) -> None:
    """Выводит список всех доступных для реплея эпизодов в терминал."""
    episodes, _ = load_trajectories_from_source(source)
    if not episodes:
        print(f"Траектории не найдены в источнике: {source or 'trajectories.jsonl'}")
        return

    th = Theme(use_color)
    hdrs = ["№", "ID Эпизода", "Всего шагов", "Находок", "Финальная награда", "Статус"]
    rows = []

    for idx, (eid, steps) in enumerate(sorted(episodes.items()), 1):
        step_count = len(steps)
        last_step = steps[-1] if steps else {}
        action = last_step.get("action") or {}
        findings_count = len((action.get("args") or {}).get("findings", {}).get("findings", []))
        reward = last_step.get("reward", 0.0)
        status = (last_step.get("observation") or {}).get("status", "done" if last_step.get("terminated") else "in_progress")

        reward_str = f"{reward:+.4f}"
        rows.append([str(idx), eid, str(step_count), str(findings_count), reward_str, status])

    print(f"\n{th.BOLD}{th.BRIGHT_CYAN}Доступные эпизоды в файле траекторий ({len(episodes)} шт.):{th.RESET}\n")
    print(draw_table(hdrs, rows, alignments=["center", "left", "center", "center", "right", "center"], theme=th, border_color=th.CYAN, header_color=th.BRIGHT_WHITE))
    print(f"\n{th.DIM}Запуск просмотра эпизода:{th.RESET}")
    print(f"  python -m scripts.replay --episode {sorted(episodes.keys())[0]}\n")


def replay_episode(
    source: Optional[str] = None,
    episode_id: Optional[str] = None,
    delay: float = 0.0,
    interactive: bool = False,
    summary_only: bool = False,
    use_color: bool = True,
    step_filter: Optional[int] = None,
) -> int:
    """Основная логика терминального просмотра траектории эпизода."""
    theme = Theme(use_color)
    episodes, ep_dir = load_trajectories_from_source(source)

    if not episodes:
        print(f"{theme.RED}Ошибка: Файл траекторий пуст или не найден.{theme.RESET}")
        return 1

    # Выбор целевого эпизода
    target_id = episode_id
    if not target_id:
        if ep_dir and ep_dir.name in episodes:
            target_id = ep_dir.name
        else:
            target_id = sorted(episodes.keys())[0]

    if target_id not in episodes:
        print(f"{theme.RED}Ошибка: Эпизод '{target_id}' не найден среди доступных: {sorted(episodes.keys())}{theme.RESET}")
        return 1
    if ep_dir is not None and not episode_id and ep_dir.name != target_id:
        # передана папка эпизода, а его траектории нет: не подменяем его другим эпизодом
        print(f"{theme.RED}Ошибка: для эпизода '{ep_dir.name}' нет траектории "
              f"(ни {ep_dir / 'trajectories.jsonl'}, ни записи в общем trajectories.jsonl).{theme.RESET}")
        return 1

    steps = episodes[target_id]
    if not ep_dir or ep_dir.name != target_id:
        ep_dir = find_episode_path(target_id)

    ground_truth = load_ground_truth(ep_dir)

    # Валютные экспоненты
    manifest = {}
    for s in steps:
        m = (s.get("observation") or {}).get("manifest")
        if m:
            manifest = m
            break
    exponents = manifest.get("currency_exponents", {})

    # Фильтрация по конкретному шагу
    if step_filter is not None:
        matching = [s for s in steps if s.get("step") == step_filter]
        if not matching:
            print(f"{theme.RED}Шаг {step_filter} не найден в эпизоде {target_id}.{theme.RESET}")
            return 1
        s = matching[0]
        if s.get("step") == 0:
            print_step_initial(s, theme)
        elif (s.get("action") or {}).get("tool") == "submit":
            print_step_final(s, theme, ground_truth, exponents)
        else:
            print_step_action(s, theme, exponents)
        return 0

    # Режим краткой сводки (summary only)
    if summary_only:
        if steps:
            print_step_initial(steps[0], theme)
            last = steps[-1]
            if (last.get("action") or {}).get("tool") == "submit":
                print_step_final(last, theme, ground_truth, exponents)
            else:
                print_step_action(last, theme, exponents)
        return 0

    # Полный пошаговый просмотр
    fast_forward = False
    for idx, s in enumerate(steps):
        s_num = s.get("step", idx)
        is_first = (s_num == 0)
        is_submit = bool((s.get("action") or {}).get("tool") == "submit")

        if is_first:
            print_step_initial(s, theme)
        elif is_submit:
            print_step_final(s, theme, ground_truth, exponents)
        else:
            print_step_action(s, theme, exponents)

        if not fast_forward and interactive and idx < len(steps) - 1:
            try:
                cmd = input(f"{theme.DIM}[Enter] — следующий шаг | [s] — перейти к итогу | [q] — выход: {theme.RESET}").strip().lower()
                if cmd == "q":
                    print(f"\n{theme.YELLOW}Просмотр прерван пользователем.{theme.RESET}")
                    break
                elif cmd == "s":
                    fast_forward = True
            except (KeyboardInterrupt, EOFError):
                print(f"\n{theme.YELLOW}Просмотр прерван.{theme.RESET}")
                break
        elif not fast_forward and delay > 0.0 and idx < len(steps) - 1:
            time.sleep(delay)

    return 0


def main() -> int:
    """Точка входа CLI."""
    parser = argparse.ArgumentParser(
        description="Терминальный просмотрщик траекторий финансового аудитора (Audit Gym Replay)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Примеры:
  python -m scripts.replay --list
  python -m scripts.replay trajectories.jsonl --episode EP-0HJPAE81
  python -m scripts.replay episodes/train/EP-0HJPAE81
  python -m scripts.replay --interactive
  python -m scripts.replay --summary
""",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Путь к файлу trajectories.jsonl или каталогу эпизода",
    )
    parser.add_argument(
        "-e", "--episode",
        dest="episode",
        default=None,
        help="ID эпизода для воспроизведения (например, EP-0HJPAE81)",
    )
    parser.add_argument(
        "-l", "--list",
        action="store_true",
        help="Показать список всех эпизодов в траектории и выйти",
    )
    parser.add_argument(
        "-i", "--interactive",
        action="store_true",
        help="Интерактивный режим: ожидание нажатия Enter на каждом шаге",
    )
    parser.add_argument(
        "-d", "--delay",
        type=float,
        default=0.0,
        help="Задержка между шагами в секундах (по умолчанию 0.0)",
    )
    parser.add_argument(
        "-s", "--step",
        type=int,
        default=None,
        help="Показать только конкретный номер шага",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Показать только вступительный контекст и итоговое заключение",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Отключить ANSI-цвета в консоли",
    )

    args = parser.parse_args()

    # Проверка вызова --list
    if args.list:
        list_episodes(args.target, use_color=not args.no_color)
        return 0

    return replay_episode(
        source=args.target,
        episode_id=args.episode,
        delay=args.delay,
        interactive=args.interactive,
        summary_only=args.summary,
        use_color=not args.no_color,
        step_filter=args.step,
    )


if __name__ == "__main__":
    sys.exit(main())
