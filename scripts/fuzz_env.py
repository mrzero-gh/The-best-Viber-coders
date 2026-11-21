"""Фазз действий среды: python -m scripts.fuzz_env [--episodes 200] [--server 20] [--seed 0].

Случайные, мусорные и пограничные действия агента (fintech-checks.html: среда не должна падать
и обязана честно считать шаги). Две части:
1. ядро AuditCore напрямую (быстро): --episodes эпизодов, в каждом до 100+ случайных шагов;
2. настоящий процесс env.server через judge_adapter.make_env: --server эпизодов и сырой протокол
   (битый JSON, не-UTF-8, длинная строка, неизвестные команды).
Инварианты каждого шага:
- step не бросает исключений (кроме GraderError на повреждённом эталоне, здесь его нет);
- наблюдение сериализуется в JSON без NaN, kind из {result, error, final};
- до конца эпизода steps_used растёт ровно на 1, steps_remaining = 100 - steps_used;
- 100-й шаг без submit завершает эпизод со status=step_limit и truncated=True;
- submit завершает эпизод, награда в [-1, 1], после конца - EPISODE_FINISHED без расхода шага;
- тот же набор действий даёт те же наблюдения (детерминизм).
Не входит в CI; итог печатается, проблемы - списком.
"""
import argparse
import json
import math
import random
import sys
import tempfile
import time
import traceback
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from env.core import MAX_STEPS, TABLES, AuditCore  # noqa: E402
from env.episode import load_public, load_truth  # noqa: E402

_JUNK = [None, True, False, 0, -1, 1.5, float("nan"), float("inf"), 10**30, "", " ", "x" * 300,
         "\u0000", "\ud800", "ledger\n", "LEDGER", "../private/truth", "c-1", "c01", "c" + "9" * 13,
         [], [1, 2], {}, {"a": 1}, [[[[[]]]]], "L-", "INV-00000000", "DOC-ZZZZZZZZ", "E-12345678"]
_EXPR = ["1+1", "0.1+0.2", "1/0", "1/3*3", "(" * 60 + "1" + ")" * 60, "-" * 150 + "1", "9" * 199,
         "1e5", "2**3", "1,5", "١٢", "  ", "", "1/" + "9" * 190, "((1)", "1)", ".5+.", "1..2",
         "99999999999999999999*99999999999999999999*99999999999999999999*999999999999999999999"]


def _ids(core):
    ep = core.episode
    out = {t: list(ep.records[t]) for t in TABLES}
    out["docs"] = list(ep.documents)
    return out


def _junk(rng):
    return rng.choice(_JUNK)


def random_action(rng, ids, truth):
    """Смесь корректных, почти корректных и мусорных действий."""
    r = rng.random()
    t = rng.choice(TABLES)
    if r < 0.05:
        return _junk(rng)  # не объект
    if r < 0.10:
        return {"tool": rng.choice(["list_records", "submit", "nope", None, 1]), "args": _junk(rng)}
    if r < 0.13:
        return {"tool": "get_record", "args": {"table": t, "id": "x"}, "extra": 1}
    if r < 0.33:
        args = {"table": rng.choice([t, t, _junk(rng)])}
        if rng.random() < 0.5:
            args["cursor"] = rng.choice([None, "c0", "c50", "c%d" % rng.randint(0, 3000), _junk(rng)])
        if rng.random() < 0.5:
            args["limit"] = rng.choice([1, 50, 51, 0, -5, True, 10.0, _junk(rng)])
        if rng.random() < 0.05:
            args["zzz"] = 1
        return {"tool": "list_records", "args": args}
    if r < 0.53:
        good = ids[t][rng.randrange(len(ids[t]))] if ids[t] else "x"
        return {"tool": "get_record", "args": {"table": rng.choice([t, t, _junk(rng)]),
                                               "id": rng.choice([good, good, _junk(rng)])}}
    if r < 0.68:
        good = rng.choice(ids["docs"]) if ids["docs"] else "x"
        return {"tool": "get_document", "args": rng.choice([{"doc_id": good}, {"doc_id": _junk(rng)}, {},
                                                            {"doc_id": good, "x": 1}])}
    if r < 0.85:
        e = rng.choice(_EXPR) if rng.random() < 0.6 else "".join(
            rng.choice("0123456789+-*/(). ") for _ in range(rng.randint(1, 40)))
        return {"tool": "calculator", "args": rng.choice([{"expression": e}, {"expression": _junk(rng)}])}
    return {"tool": "submit", "args": random_submit_args(rng, truth, ids)}


def random_submit_args(rng, truth, ids):
    r = rng.random()
    if r < 0.15:
        return rng.choice([{}, {"findings": _junk(rng)}, {"findings": {}, "x": 1}])
    eid = truth["episode_id"] if rng.random() < 0.9 else _junk(rng)
    findings = []
    for _ in range(rng.choice([0, 1, 2, 5, 40])):
        if truth["issues"] and rng.random() < 0.5:
            i = rng.choice(truth["issues"])
            f = {"type": i["type"], "affected_ids": list(i["affected_ids"]),
                 "claimed_amount_minor": i["expected_amount_minor"], "currency": i["currency"],
                 "evidence_ids": list(i["evidence_sets"][0]), "explanation": "fuzz"}
        else:
            f = {"type": rng.choice(["duplicate_invoice", "fx_conversion", "made_up", "", _junk(rng)]),
                 "affected_ids": rng.choice([[rng.choice(ids["ledger"] or ["x"])], [], _junk(rng)]),
                 "claimed_amount_minor": rng.choice([0, 1, -1, 10**18, 1.5, "1", _junk(rng)]),
                 "currency": rng.choice(["KZT", "kzt", "", _junk(rng)]),
                 "evidence_ids": rng.choice([[rng.choice(ids["docs"] or ["x"])], _junk(rng)]),
                 "explanation": rng.choice(["", "x" * 3000, _junk(rng)])}
        if rng.random() < 0.1:
            f.pop(rng.choice(list(f)))
        if rng.random() < 0.05:
            f["extra"] = 1
        findings.append(f)
    if rng.random() < 0.1 and findings:
        findings.append(findings[0])  # дубль находки
    return {"findings": {"episode_id": eid, "findings": findings}}


def _json_ok(obj) -> bool:
    try:
        json.dumps(obj, allow_nan=False)
        return True
    except (TypeError, ValueError):
        return False


STATUS = Counter()


def run_core_episode(ep_dir, rng, bad, max_actions=130, submit_share=None):
    """Один эпизод ядра со случайными действиями; возвращает список наблюдений (для детерминизма)."""
    public, truth = load_public(ep_dir), load_truth(ep_dir)
    core = AuditCore(public, truth, mode="train")
    ids = _ids(core)
    trace, actions = [], []
    finished = False
    for n in range(max_actions):
        action = random_action(rng, ids, truth)
        if submit_share is not None and isinstance(action, dict) and action.get("tool") == "submit"                 and rng.random() > submit_share:
            action = {"tool": "calculator", "args": {"expression": "1+1"}}  # реже submit: дойти до лимита
        actions.append(action)
        before = core.steps_used
        try:
            obs, reward, term, trunc, info = core.step(action)
        except Exception as exc:  # noqa: BLE001
            bad(f"исключение {type(exc).__name__}: {exc} на {repr(action)[:200]}")
            return trace, actions
        if not _json_ok(obs) or not _json_ok(info) or not isinstance(reward, float) or math.isnan(reward):
            bad(f"несериализуемый ответ на {repr(action)[:200]}")
        kind = obs.get("kind")
        trace.append(json.dumps([obs, reward, term, trunc, info], sort_keys=True, default=str))
        if finished:
            if kind != "error" or obs["error"]["code"] != "EPISODE_FINISHED" or core.steps_used != before:
                bad(f"после конца: {obs}")
            continue
        if core.steps_used != before + 1 and not (kind == "final" and obs.get("status") == "time_limit"):
            bad(f"steps_used {before} -> {core.steps_used} на {repr(action)[:120]}")
        if obs.get("steps_remaining") != MAX_STEPS - core.steps_used:
            bad(f"steps_remaining {obs.get('steps_remaining')} при steps_used {core.steps_used}")
        if kind not in ("result", "error", "final"):
            bad(f"kind {kind}")
        if kind == "final":
            finished = True
            STATUS[obs["status"]] += 1
            if not -1.0 <= reward <= 1.0:
                bad(f"награда {reward}")
            st = obs["status"]
            if st == "step_limit" and not (trunc and not term and core.steps_used == MAX_STEPS):
                bad(f"step_limit: term={term} trunc={trunc} steps={core.steps_used}")
            if st in ("submitted", "invalid_findings") and not (term and not trunc):
                bad(f"{st}: term={term} trunc={trunc}")
            if st == "invalid_findings" and reward > 0:
                bad(f"invalid_findings с наградой {reward}")
        elif term or trunc or reward != 0.0:
            bad(f"не final, но term={term} trunc={trunc} reward={reward}")
    return trace, actions


def replay(ep_dir, actions):
    public, truth = load_public(ep_dir), load_truth(ep_dir)
    core = AuditCore(public, truth, mode="train")
    return [json.dumps(list(core.step(a)), sort_keys=True, default=str) for a in actions]


# ---------------------------------------------------------------- процесс сервера

def run_server_part(eps, n, rng, bad):
    import subprocess
    import judge_adapter as ja
    from env.client import EnvError
    stats = Counter()
    runs = tempfile.mkdtemp(prefix="fuzzenv-runs-")
    for k in range(n):
        ep = eps[k % len(eps)]
        env = ja.JudgeEnv(runs_dir=runs)  # как make_env, но журнал во временной папке
        try:
            first, info = env.reset(0, {"mode": rng.choice(["closed", "train"]), "episode_dir": str(ep)})
            truth = load_truth(ep)
            public = load_public(ep)
            ids = {t: list(public.records[t]) for t in TABLES}
            ids["docs"] = list(public.documents)
            prev, done = first["remaining_steps"], False
            for _ in range(110):
                a = random_action(rng, ids, truth)
                if isinstance(a, dict) and "tool" in a and set(a) == {"tool", "args"} and rng.random() < 0.5:
                    a = {"name": a["tool"], "args": a["args"]}  # формат адаптера
                try:
                    obs, reward, term, trunc, inf = env.step(a)
                except (TypeError, ValueError) as exc:
                    # несериализуемое (NaN допустим) в клиенте: до сервера не дошло, шаг не потрачен
                    stats["client_reject"] += 1
                    continue
                except EnvError as exc:
                    bad(f"сервер: EnvError {exc.code} на {repr(a)[:150]}")
                    break
                stats["steps"] += 1
                if done:
                    if obs["error"] is None or obs["error"]["code"] != "EPISODE_FINISHED" \
                            or obs["remaining_steps"] != prev:
                        bad(f"сервер после конца: {obs}")
                    continue
                if obs["remaining_steps"] != prev - 1:
                    bad(f"сервер: remaining {prev} -> {obs['remaining_steps']} на {repr(a)[:120]}")
                prev = obs["remaining_steps"]
                if term or trunc:
                    done = True
                    stats["finished"] += 1
                    if not -1.0 <= reward <= 1.0:
                        bad(f"сервер: награда {reward}")
                    if info["mode"] == "closed" and reward != 0.0:
                        bad("сервер: награда в закрытом режиме")
        except Exception as exc:  # noqa: BLE001
            bad(f"сервер: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}")
        finally:
            env.close()
    # сырой протокол: мусор не должен ронять сервер; после мусора сервер отвечает дальше
    raw = [b"{", b"\xff\xfe\n"[:-1], b"[]", b'{"cmd": 1}', b'{"cmd": "step", "action": {}}',
           b'{"cmd": "reset", "seed": true, "config": {}}', b'{"cmd": "reset", "seed": 1, "config": []}',
           b"[" * 100000 + b"]" * 100000, b"x" * (1024 * 1024 + 10), b'{"cmd": "nope"}',
           b'{"cmd": "reset", "seed": 1e400, "config": {"mode": "train"}}', b"\x00", b"null",
           b'{"cmd": "reset", "seed": 1, "config": {"mode": "train", "size": "big"}}',
           b'{"cmd": "reset", "seed": ' + b"9" * 5000 + b', "config": {"mode": "train"}}',
           # экранированный одиночный суррогат: валидный JSON, но не UTF-8 (найдено этим фаззом)
           b'{"cmd": "reset", "seed": 1, "config": {"mode": "train", "config_version": "\\ud800"}}',
           b'{"cmd": "step", "action": {"tool": "get_record", "args": {"table": "ledger", "id": "\\ud800"}}}']
    proc = subprocess.Popen([sys.executable, "-m", "env.server", "--runs-dir", tempfile.mkdtemp()],
                            cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    try:
        for line in raw:
            proc.stdin.write(line + b"\n")
            proc.stdin.flush()
            resp = proc.stdout.readline()
            try:
                msg = json.loads(resp)
            except ValueError:
                bad(f"сырой протокол: не JSON в ответ на {line[:40]!r}: {resp[:100]!r}")
                break
            if "error" not in msg:
                bad(f"сырой протокол: нет ошибки на {line[:40]!r}: {msg}")
            elif msg["error"]["code"] == "INTERNAL":
                bad(f"сырой протокол: INTERNAL на {line[:40]!r}")
            stats["raw"] += 1
        proc.stdin.write(b'{"cmd": "close"}\n')
        proc.stdin.flush()
        if json.loads(proc.stdout.readline()) != {"ok": True}:
            bad("сырой протокол: close после мусора не ответил ok")
    finally:
        proc.kill()
        proc.wait()
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episodes", type=int, default=200)
    ap.add_argument("--server", type=int, default=20)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args(argv)
    eps = sorted(p for s in ("train", "dev") for p in (ROOT / "episodes" / s).glob("EP-*")
                 if (p / "private" / "truth.json").exists())
    rng = random.Random(a.seed)
    problems = []
    t0 = time.monotonic()
    nondet = 0
    for k in range(a.episodes):
        ep = eps[k % len(eps)]
        found = []
        # каждый третий эпизод почти без submit, чтобы проверялся лимит в 100 шагов
        trace, actions = run_core_episode(ep, rng, found.append, submit_share=0.02 if k % 3 == 2 else None)
        if not found and replay(ep, actions) != trace:
            found.append("недетерминизм: повтор тех же действий дал другие наблюдения")
            nondet += 1
        problems += [f"{ep.name} #{k}: {p}" for p in found]
    print(f"ядро: эпизодов {a.episodes} за {time.monotonic() - t0:.0f} с, итоги {dict(STATUS)}, "
          f"проблем {len(problems)}")
    t1 = time.monotonic()
    sp = []
    stats = run_server_part(eps, a.server, rng, sp.append) if a.server else Counter()
    print(f"сервер: эпизодов {a.server} за {time.monotonic() - t1:.0f} с, {dict(stats)}, проблем {len(sp)}")
    problems += sp
    for p, c in Counter(p.split(": ", 1)[-1][:110] for p in problems).most_common(20):
        print(f"  {c:4d} x {p}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
