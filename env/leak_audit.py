"""Аудит утечек среды: ни меток эталона, ни путей к файлам в ответах (задача #20).

Что делает: запускает настоящий сервер среды (env.client.AuditEnv), прогоняет честные, оракульные,
ошибочные и «мусорные» сценарии, собирает ВСЕ ответы и сообщения EnvError и ищет в них
метки эталона (private/) и признаки путей. Отдельно проверяет eval-инварианты.

CLI: python -m env.leak_audit [--seeds 1,2,3] [--out FILE]. Код возврата 0 - чисто, 1 - есть находки.
Только stdlib.
"""
import argparse
import copy
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

from common.jsonio import canonical_dumps
from env.client import AuditEnv, EnvError
from env.core import MAX_STEPS, TABLES, TOOLS
from env.episode import load_public, load_truth

ROOT = Path(__file__).resolve().parent.parent

# Ключи эталона и служебные слова, которых не должно быть в выходе среды
KEY_MARKERS = ("issue_id", "evidence_sets", "expected_amount_minor", "amount_tolerance_minor", "distractor",
               "mutation", "provenance", "truth", "public_sha256", "episode_sha256", "generator_version",
               "errors_injected", "private")
# Общие признаки путей и трейсбеков
PATH_SIGNS = ("Traceback", 'File "', '.py"', "\\Users\\", "/home/", "/tmp/")
TEMP_PREFIX = "auditgym-"

# Слова-подсказки, которых не должно быть в public/ (без учёта регистра)
HINT_WORDS = ("дубл", "duplicate", "ошибк", "error", "cutoff", "wrong", "неверн", "canary", "distractor",
              "ловушк", "fake")

_ID_RE = re.compile(r"[A-Z]{2,4}-[0-9A-Z]{8}")
_HEX_RE = re.compile(r"[0-9a-f]{32,}")
_PUBLIC_FILES = ("manifest.json", "policy.json", "ledger.csv", "invoices.jsonl", "payments.jsonl", "fx.csv")


# ---------- метки ----------
def _strings(obj):
    """Все строки (и ключи словарей) вложенной структуры."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)


def _read(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _public_strings(episode_dir) -> set:
    """Все строки открытых данных: публичные ID и значения законно видны агенту."""
    ep = load_public(episode_dir)
    out = set()
    for part in (ep.manifest, ep.policy, ep.ledger, ep.invoices, ep.payments, ep.fx):
        out.update(_strings(part))
    return out


def private_markers(episode_dir) -> set:
    """Строки, которые никогда не должны появиться в выходе среды."""
    priv = Path(episode_dir) / "private"
    truth, prov, gen = _read(priv / "truth.json"), _read(priv / "provenance.json"), _read(priv / "generation.json")
    markers = set(KEY_MARKERS)
    for i in truth.get("issues", []):
        markers.add(i["issue_id"])
        if i.get("scenario"):
            markers.add(i["scenario"])
    for d in truth.get("distractors", []):
        markers.add(d["distractor_id"])
        markers.add(d["kind"])
    for key in ("episode_sha256", "generator_version"):
        if isinstance(gen.get(key), str):
            markers.add(gen[key])
    if isinstance(prov.get("clean_history_sha256"), str):
        markers.add(prov["clean_history_sha256"])
    # Публичные значения (ID, ключи политики вроде amount_tolerance_minor) и имена инструментов/таблиц
    # законно видны агенту: из меток их исключаем
    legit = _public_strings(episode_dir) | set(TABLES) | set(TOOLS)
    markers -= legit
    for s in _strings(prov.get("mutations", [])):
        if len(s) >= 6 and s not in legit:
            markers.add(s)
    return markers


def path_markers(*paths) -> set:
    """Пути и общие признаки путей/трейсбеков."""
    markers = set(PATH_SIGNS)
    markers.add(TEMP_PREFIX)
    markers.add(tempfile.gettempdir())
    markers.add(str(ROOT))
    markers.add(ROOT.as_posix())
    for p in paths:
        if p is None:
            continue
        markers.add(str(p))
        markers.add(Path(p).as_posix())
    return {m for m in markers if m}


def _is_exact(marker: str) -> bool:
    """ID и хэши ищем с учётом регистра, словесные метки - без учёта."""
    return bool(_ID_RE.fullmatch(marker) or _HEX_RE.fullmatch(marker))


def _variants(marker: str):
    """Метка и её JSON-экранированная форма (кавычки и обратные слэши в сериализованном ответе)."""
    yield marker
    esc = json.dumps(marker, ensure_ascii=False)[1:-1]
    if esc != marker:
        yield esc


def scan(texts, markers) -> list:
    """Ищет метки в строках; возвращает [(метка, фрагмент <= 80 символов)]."""
    exact, loose = [], []
    for m in markers:
        for v in _variants(m):
            (exact if _is_exact(m) else loose).append((m, v if _is_exact(m) else v.lower()))
    found = []
    for text in texts:
        low = None
        for m, v in exact:
            pos = text.find(v)
            if pos >= 0:
                found.append((m, _snippet(text, pos, len(v))))
        for m, v in loose:
            if low is None:
                low = text.lower()
            pos = low.find(v)
            if pos >= 0:
                found.append((m, _snippet(text, pos, len(v))))
    return found


def _snippet(text: str, pos: int, n: int) -> str:
    """Фрагмент вокруг находки, не длиннее 80 символов."""
    half = max(0, (80 - n) // 2)
    s = text[max(0, pos - half):pos + n + half]
    return s[:80]


# ---------- сбор ответов ----------
class _Sink:
    """Копит тексты ответов, строки траектории, счётчики и нарушения одной ячейки аудита."""

    def __init__(self, mode):
        self.mode = mode
        self.texts = []        # (сценарий, текст)
        self.trajectory = []   # строки траектории из ответов среды (action не пишем: это ввод самого агента)
        self.violations = []
        self.steps = 0
        self.scenario = "-"
        self.ep_id = None
        self._step_no = 0

    def _eval_checks(self, info, text, reward=None):
        if self.mode != "eval":
            return
        if set(info) != {"episode_id", "mode"}:
            self.violations.append("[%s] eval: info содержит лишние ключи: %s" % (self.scenario, sorted(info)))
        if reward is not None and reward != 0.0:
            self.violations.append("[%s] eval: reward %r вместо 0.0" % (self.scenario, reward))
        if '"metrics"' in text:
            self.violations.append("[%s] eval: в ответе есть metrics" % self.scenario)

    def reset(self, env, seed, cfg):
        obs, info = env.reset(seed, cfg)
        text = canonical_dumps({"observation": obs, "info": info})
        self.texts.append((self.scenario, text))
        self._eval_checks(info, text)
        self.ep_id = info["episode_id"]
        self._step_no = 0
        self.trajectory.append(canonical_dumps(
            {"episode_id": self.ep_id, "step": 0, "action": None, "observation": obs,
             "reward": 0.0, "terminated": False, "truncated": False}))
        return obs, info

    def step(self, env, action):
        """Шаг; ошибки протокола тоже попадают в тексты. Возвращает (obs, reward, term, trunc, info) или None."""
        self.steps += 1
        self._step_no += 1
        try:
            obs, reward, term, trunc, info = env.step(action)
        except EnvError as e:
            self.texts.append((self.scenario, "EnvError %s %s" % (e.code, e.message)))
            return None
        text = canonical_dumps({"observation": obs, "reward": reward, "terminated": term,
                                "truncated": trunc, "info": info})
        self.texts.append((self.scenario, text))
        self._eval_checks(info, text, reward)
        self.trajectory.append(canonical_dumps(
            {"episode_id": self.ep_id, "step": self._step_no, "action": None, "observation": obs,
             "reward": reward, "terminated": term, "truncated": trunc}))
        return obs, reward, term, trunc, info

    def raw(self, env, data: bytes):
        """Сырые байты в stdin сервера (протокольный мусор); ответ сохраняем как есть."""
        proc = env._proc
        proc.stdin.write(data)
        proc.stdin.flush()
        line = proc.stdout.readline()
        text = line.decode("utf-8", "replace")
        self.texts.append((self.scenario, text))
        if not line:
            self.violations.append("[%s] сервер не ответил на мусор" % self.scenario)
        return text


def _expect(sink, cond, what):
    if not cond:
        sink.violations.append("[%s] ожидалось: %s" % (sink.scenario, what))


# ---------- сценарии ----------
def _honest(sink, env, seed, cfg):
    """a) честный проход: все таблицы с курсорами, get_record/get_document, calculator, пустой submit."""
    sink.scenario = "honest"
    _obs, info = sink.reset(env, seed, cfg)
    ids = {}
    for table in TABLES:
        cursor = None
        for page in range(3):
            args = {"table": table} if cursor is None else {"table": table, "cursor": cursor}
            out = sink.step(env, {"tool": "list_records", "args": args})
            if out is None or out[0]["kind"] != "result":
                break
            res = out[0]["result"]
            ids.setdefault(table, []).extend(res["records"])
            cursor = res["next_cursor"]
            if cursor is None:
                break
    pk = {"ledger": "row_id", "invoices": "invoice_id", "payments": "payment_id"}
    for table, key in pk.items():
        for row in ids.get(table, [])[:3]:
            sink.step(env, {"tool": "get_record", "args": {"table": table, "id": row[key]}})
    for row in ids.get("fx", [])[:2]:
        rid = "FX:%s:%s:%s" % (row["date"], row["source_currency"], row["target_currency"])
        sink.step(env, {"tool": "get_record", "args": {"table": "fx", "id": rid}})
    for table in ("invoices", "payments"):
        for row in ids.get(table, [])[:3]:
            sink.step(env, {"tool": "get_document", "args": {"doc_id": row["doc_id"]}})
    sink.step(env, {"tool": "calculator", "args": {"expression": "(1 + 2) * 3 / 4"}})
    return info


def _bad_input(sink, env):
    """c) неверный ввод (эпизод уже начат, ничего не завершает)."""
    sink.scenario = "bad_input"
    long_id = "X" * 500
    actions = [
        {"tool": "nope", "args": {}},
        {"tool": "list_records", "args": {"table": "nope"}},
        {"tool": "list_records", "args": {"table": "ledger", "zzz": 1}},
        {"tool": "list_records", "args": {"table": "ledger", "cursor": "c99999999"}},
        {"tool": "list_records", "args": {"table": "ledger", "cursor": "abc"}},
        {"tool": "list_records", "args": {"table": "ledger", "cursor": "c-1"}},
        {"tool": "list_records", "args": {"table": "ledger", "cursor": 5}},
        {"tool": "list_records", "args": {"table": "ledger", "limit": 0}},
        {"tool": "list_records", "args": {"table": "ledger", "limit": 51}},
        {"tool": "list_records", "args": {"table": "ledger", "limit": True}},
        {"tool": "get_record", "args": {"table": "ledger", "id": "L-" + long_id}},
        {"tool": "get_record", "args": {"table": "invoices", "id": "INV-00000000"}},
        {"tool": "get_record", "args": {"table": "nope", "id": "x"}},
        {"tool": "get_record", "args": {"table": "ledger"}},
        {"tool": "get_document", "args": {"doc_id": "DOC-" + long_id}},
        {"tool": "get_document", "args": {"doc_id": 5}},
        {"tool": "calculator", "args": {"expression": "__import__('os')"}},
        {"tool": "calculator", "args": {"expression": "1/0"}},
        {"tool": "calculator", "args": {"expression": "(" * 300}},
        {"tool": "calculator", "args": {"expression": "9" * 300}},
        {"tool": "calculator", "args": {"expression": "open('/etc/passwd')"}},
        {"tool": "submit", "args": {}},
        {"tool": "submit", "args": {"findings": [], "extra": 1}},
        "строка вместо действия",
        None,
        [],
        {"tool": "list_records"},
        {"tool": "list_records", "args": []},
        {"tool": 5, "args": {}},
        {"tool": "calculator", "args": {"expression": "1+1"}, "extra": True},
    ]
    for a in actions:
        out = sink.step(env, a)
        _expect(sink, out is not None and out[0]["kind"] in ("error", "result"), "ответ среды на неверный ввод")


def _garbage(sink, env):
    """e) протокольный мусор через stdin сервера."""
    sink.scenario = "garbage"
    for data in (b"not json at all\n", b'{"cmd": "zzz"}\n', b'{"cmd": "step"}\n', b"[1, 2]\n", b"\xff\xfe\n",
                 b'{"cmd": "reset", "seed": 1, "config": {"mode": "x"}}\n',
                 b'{"cmd": "reset", "seed": true, "config": {"mode": "eval"}}\n',
                 b'{"cmd": "close", "extra": 1}\n'):
        text = sink.raw(env, data)
        _expect(sink, '"error"' in text, "ошибка протокола на мусор")


def _finish_checks(sink, env, ep_id, status):
    """После завершения: step даёт EPISODE_FINISHED."""
    out = sink.step(env, {"tool": "calculator", "args": {"expression": "1"}})
    _expect(sink, out is not None and out[0].get("error", {}).get("code") == "EPISODE_FINISHED",
            "EPISODE_FINISHED после завершения (%s)" % status)


def _oracle_findings(truth):
    return {"episode_id": truth["episode_id"], "findings": [
        {"type": i["type"], "affected_ids": i["affected_ids"], "claimed_amount_minor": i["expected_amount_minor"],
         "currency": i["currency"], "evidence_ids": i["evidence_sets"][0], "explanation": "oracle"}
        for i in truth["issues"]]}


def run_cell(make_env, mode, seed, cfg, truth=None, sink=None, generated=True):
    """Все сценарии a-e на одном процессе среды. truth нужен для сценария oracle (если None - пропуск).

    generated=False: сервер запущен с --episode, плохой config генерацию не ломает (сценарий bad_config пропускается).
    """
    cfg = copy.deepcopy(cfg)
    cfg["mode"] = mode
    sink = sink or _Sink(mode)
    env = make_env()
    try:
        # NO_EPISODE: step до reset
        sink.scenario = "no_episode"
        sink.step(env, {"tool": "calculator", "args": {"expression": "1"}})
        # a + c + e и пустой submit
        info = _honest(sink, env, seed, cfg)
        _bad_input(sink, env)
        _garbage(sink, env)
        sink.scenario = "honest"
        out = sink.step(env, {"tool": "submit", "args": {"findings": {"episode_id": info["episode_id"], "findings": []}}})
        _expect(sink, out is not None and out[0].get("status") == "submitted", "status submitted при пустом submit")
        _finish_checks(sink, env, info["episode_id"], "submitted")
        # b) oracle
        if truth is not None:
            sink.scenario = "oracle"
            _obs, info = sink.reset(env, seed, cfg)
            out = sink.step(env, {"tool": "calculator", "args": {"expression": "2*2"}})
            out = sink.step(env, {"tool": "submit", "args": {"findings": _oracle_findings(truth)}})
            _expect(sink, out is not None and out[0].get("status") == "submitted", "status submitted у oracle")
            if mode == "eval" and out is not None:
                _expect(sink, out[1] == 0.0, "reward 0.0 у oracle в eval")
        # c) неверные findings: 31 вывод, не объект, лишние поля
        bad = [
            ("findings31", {"episode_id": "EP-00000000", "findings": [
                {"type": "duplicate_invoice", "affected_ids": ["INV-00000000"], "claimed_amount_minor": 1,
                 "currency": "KZT", "evidence_ids": ["DOC-00000000"], "explanation": "x"}] * 31}),
            ("findings_not_object", "строка"),
            ("findings_extra", {"episode_id": "EP-00000000", "findings": [], "extra": 1}),
        ]
        for name, f in bad:
            sink.scenario = name
            sink.reset(env, seed, cfg)
            out = sink.step(env, {"tool": "submit", "args": {"findings": f}})
            _expect(sink, out is not None and out[0].get("status") == "invalid_findings", "invalid_findings")
        # d) усечение по лимиту шагов
        sink.scenario = "step_limit"
        sink.reset(env, seed, cfg)
        out = None
        for _ in range(MAX_STEPS):
            out = sink.step(env, {"tool": "calculator", "args": {"expression": "1+1"}})
        _expect(sink, out is not None and out[0].get("status") == "step_limit" and out[3] is True,
                "step_limit с truncated=True на 100-м шаге")
        _finish_checks(sink, env, None, "step_limit")
        # плохой config (в eval: GENERATION_FAILED; в train не делаем - сервер печатает трейсбек в stderr)
        if mode == "eval" and generated:
            sink.scenario = "bad_config"
            try:
                env.reset(seed, {"mode": "eval", "size": {"ledger_rows": [10 ** 9, 1]}})
            except EnvError as e:
                sink.texts.append((sink.scenario, "EnvError %s %s" % (e.code, e.message)))
                _expect(sink, e.code == "GENERATION_FAILED", "GENERATION_FAILED на плохой config")
            else:
                sink.violations.append("[bad_config] ожидалось: GENERATION_FAILED")
            sink.step(env, {"tool": "calculator", "args": {"expression": "1"}})  # NO_EPISODE
    finally:
        env.close()
    return sink


def _temp_dirs() -> set:
    return {p.name for p in Path(tempfile.gettempdir()).glob(TEMP_PREFIX + "*")}


def run_audit(seeds, configs, runs_dir) -> dict:
    """Полный аудит: seed x config x {train, eval}. configs - {имя: config}."""
    runs_dir = Path(runs_dir)
    t0 = time.monotonic()
    report = {"episodes": 0, "steps": 0, "responses": 0, "leaks": [], "violations": [], "cells": 0}
    from generator.build import generate_episode
    for seed in seeds:
        for name, cfg in configs.items():
            tmp = Path(tempfile.mkdtemp(prefix="leakaudit-"))
            try:
                gen_cfg = copy.deepcopy(cfg)
                gen_cfg["mode"] = "train"
                ep_dir = generate_episode(seed, gen_cfg, tmp)
                markers = private_markers(ep_dir) | path_markers(runs_dir, tmp, ep_dir)
                truth = load_truth(ep_dir)
                for mode in ("train", "eval"):
                    sink = run_cell(lambda: AuditEnv(runs_dir=runs_dir, run_id="audit-%s-%s-%s" % (seed, name, mode)),
                                    mode, seed, cfg, truth)
                    report["cells"] += 1
                    report["steps"] += sink.steps
                    report["responses"] += len(sink.texts)
                    report["episodes"] += sum(1 for _s, t in sink.texts if '"kind":"initial"' in t)
                    for scen, text in sink.texts:
                        for marker, snip in scan([text], markers):
                            report["leaks"].append({"seed": seed, "config": name, "mode": mode,
                                                    "scenario": scen, "marker": marker, "snippet": snip})
                    for v in sink.violations:
                        report["violations"].append("seed=%s config=%s mode=%s %s" % (seed, name, mode, v))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
    report["seconds"] = round(time.monotonic() - t0, 1)
    report["ok"] = not report["leaks"] and not report["violations"]
    return report


# ---------- канарейки ----------
CANARY_GEN = "CANARY-GENERATION-7Q4Z"
CANARY_PROV = "CANARY-PROVENANCE-7Q4Z"


def make_canary_episode(src_episode_dir, dst_dir):
    """Копия эпизода с уникальными метками в private/. Возвращает (путь, список меток). public/ не трогаем."""
    src = Path(src_episode_dir)
    dst = Path(dst_dir) / src.name
    shutil.copytree(src, dst)
    priv = dst / "private"
    truth = _read(priv / "truth.json")
    mapping = {}
    for n, i in enumerate(truth["issues"], 1):
        mapping[i["issue_id"]] = "ISS-CANARY%02d" % n
    for n, d in enumerate(truth["distractors"], 1):
        mapping[d["distractor_id"]] = "DST-CANARY%02d" % n
    for name in ("truth.json", "provenance.json", "generation.json"):
        text = (priv / name).read_text(encoding="utf-8")
        for old, new in mapping.items():
            text = text.replace(old, new)
        data = json.loads(text)
        if name == "generation.json":
            data["canary"] = CANARY_GEN
        elif name == "provenance.json":
            data["canary"] = CANARY_PROV
        (priv / name).write_text(canonical_dumps(data) + "\n", encoding="utf-8", newline="\n")
    return dst, sorted(mapping.values()) + [CANARY_GEN, CANARY_PROV]


# ---------- аудит public-данных генератора ----------
def public_hints(episode_dir) -> list:
    """Подсказки в public/: ID эталона, названия сценариев/типов/kind, слова-подсказки.

    Возвращает [(файл, поле, подсказка, пример)], без повторов по (файл, поле, подсказка).
    """
    import csv
    ep = Path(episode_dir)
    truth = load_truth(ep)
    names = {"scenario": set(), "kind": set(), "type": set()}
    for i in truth["issues"]:
        names["type"].add(i["type"])
        if i.get("scenario"):
            names["scenario"].add(i["scenario"])
    for d in truth["distractors"]:
        names["kind"].add(d["kind"])
    needles = list(HINT_WORDS) + sorted(set().union(*names.values()))
    needles += ["duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff", "partial_fx_payment_at_cutoff",
                "duplicate_invoice_wrong_fx", "split_payment", "refund", "legit_recurring", "late_evening_local"]
    needles = sorted(set(needles))
    id_re = re.compile(r"(ISS|DST)-[0-9A-Z]{8}")
    found, seen = [], set()

    def check(file, field, text):
        low = text.lower()
        hits = [n for n in needles if n.lower() in low]
        if id_re.search(text):
            hits.append("ISS-/DST- ID")
        for h in hits:
            key = (file, field, h)
            if key not in seen:
                seen.add(key)
                found.append((file, field, h, text[:80]))

    def walk(file, prefix, obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                check(file, prefix + "." + k if prefix else k, k)
                walk(file, prefix + "." + k if prefix else k, v)
        elif isinstance(obj, list):
            for v in obj:
                walk(file, prefix + "[]", v)
        elif isinstance(obj, str):
            check(file, prefix, obj)

    pub = ep / "public"
    for name in _PUBLIC_FILES:
        path = pub / name
        if name.endswith(".json"):
            walk(name, "", _read(path))
        elif name.endswith(".jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    walk(name, "", json.loads(line))
        else:
            with open(path, encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    for col, val in row.items():
                        check(name, col, val or "")
    return found


# ---------- CLI ----------
def _configs():
    from generator.config import DEFAULT_CONFIG, INTERACTIONS
    inter = copy.deepcopy(DEFAULT_CONFIG)
    inter["interactions"] = {s: {"enabled": True, "count": [1, 1]} for s in INTERACTIONS}
    return {"default": copy.deepcopy(DEFAULT_CONFIG), "interactions": inter}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="env.leak_audit", description="Аудит утечек среды Audit Gym")
    ap.add_argument("--seeds", default="1,2,3", help="seed через запятую (по умолчанию 1,2,3)")
    ap.add_argument("--out", default=None, help="записать JSON-отчёт в файл")
    args = ap.parse_args(argv)
    seeds = [int(x) for x in args.seeds.split(",") if x.strip()]
    with tempfile.TemporaryDirectory() as tmp:
        report = run_audit(seeds, _configs(), Path(tmp) / "runs")
    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Аудит утечек: seed %s, ячеек %d, эпизодов %d, шагов %d, ответов %d, %.1f с"
          % (seeds, report["cells"], report["episodes"], report["steps"], report["responses"], report["seconds"]))
    print("Утечек: %d, нарушений eval-инвариантов и сценариев: %d" % (len(report["leaks"]), len(report["violations"])))
    for leak in report["leaks"][:20]:
        print("  УТЕЧКА seed=%s %s/%s [%s] метка %r: %s" % (leak["seed"], leak["config"], leak["mode"],
                                                          leak["scenario"], leak["marker"], leak["snippet"]))
    for v in report["violations"][:20]:
        print("  НАРУШЕНИЕ", v)
    print("Результат:", "ЧИСТО" if report["ok"] else "ЕСТЬ НАХОДКИ")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
