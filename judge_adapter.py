"""Адаптер жюри FinTech (genesis-exe.com/fintech-checks.html, «Единый способ подключения»).

Три точки входа:

    generate_episode(seed, config, output_dir) -> None
    make_env() -> объект с reset(seed, config) и step(action)
    score_episode(episode_dir, findings, inspection_steps) -> dict

Среда работает в отдельном процессе `python -m env.server` (см. docs/contract.md, раздел 2).
Адаптер только переводит формат: действие {"name", "args"} -> наше {"tool", "args"},
наши наблюдения -> {"task", "manifest", "policy", "remaining_steps"} и
{"result", "error", "remaining_steps"}. Действия он не выбирает и смысл задачи не меняет.
Каталог эпизода, truth и оценщик остаются в процессе жюри и в процессе сервера, политике
передаются только наблюдения.

Проверка вручную: `python judge_adapter.py` (генерирует эпизод, проходит его и печатает итог).
"""
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from env.client import AuditEnv, EnvError
from env.core import TABLES  # канонические имена таблиц: ledger, invoices, payments, fx

ACTIONS = ("list_records", "get_record", "get_document", "calculator", "submit")
# режимы жюри -> режимы среды
_MODES = {"closed": "eval", "eval": "eval", "train": "train", "open": "train"}
# тексты ошибок завершения (без путей, seed и данных эталона)
_FINAL_ERRORS = {
    "invalid_findings": {"code": "INVALID_FINDINGS", "message": "Некорректная структура findings."},
    "step_limit": {"code": "STEP_LIMIT", "message": "Лимит шагов исчерпан."},
    "time_limit": {"code": "TIME_LIMIT", "message": "Лимит времени эпизода исчерпан."},
}


def _runs_dir() -> Path:
    return Path(os.environ.get("AUDIT_GYM_RUNS_DIR") or "runs")


def _env_mode(mode) -> str:
    if mode not in _MODES:
        raise ValueError("config.mode: closed или train")
    return _MODES[mode]


# ------------------------------------------------------------------ генерация

def generate_episode(seed: int, config: dict, output_dir: str) -> None:
    """Генерирует эпизод прямо в output_dir: public/ (manifest, ledger, invoices, payments,
    fx, policy) и private/ (truth, provenance, generation). Обёртка над generator.build."""
    from generator.build import generate_episode as _generate
    cfg = dict(config or {})
    if "mode" in cfg:
        cfg["mode"] = _env_mode(cfg["mode"])
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".gen-", dir=str(out)))
    try:
        ep_dir = _generate(seed, cfg, tmp)
        for part in ("public", "private"):
            shutil.move(str(ep_dir / part), str(out / part))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ среда

def _result_payload(tool, result):
    """Наш результат инструмента -> каноническое представление адаптера."""
    if tool == "list_records":
        return {"records": result["records"], "next_cursor": result["next_cursor"]}
    if tool == "get_record":
        return result["record"]
    if tool == "get_document":
        return result["document"]
    if tool == "calculator":
        return result["value"]  # десятичная строка, например "0.3"
    return result


def _translate(obs: dict) -> dict:
    """Наблюдение после step -> {"result", "error", "remaining_steps"}."""
    kind = obs.get("kind")
    remaining = obs.get("steps_remaining", 0)
    if kind == "result":
        return {"result": _result_payload(obs["tool"], obs["result"]), "error": None,
                "remaining_steps": remaining}
    if kind == "error":
        return {"result": None, "error": dict(obs["error"]), "remaining_steps": remaining}
    # kind == "final": submit принят, отклонён по структуре или эпизод обрезан
    status = obs.get("status")
    if status == "submitted":
        return {"result": {"accepted": True}, "error": None, "remaining_steps": remaining}
    return {"result": None, "error": dict(_FINAL_ERRORS.get(status, _FINAL_ERRORS["step_limit"])),
            "remaining_steps": remaining}


class JudgeEnv:
    """Среда для политик жюри. Каждый reset запускает новый процесс сервера:
    состояние прошлого эпизода не переживает reset."""

    def __init__(self, runs_dir=None):
        self.runs_dir = Path(runs_dir) if runs_dir else _runs_dir()
        self.run_id = "judge-%s-%d" % (time.strftime("%Y%m%d-%H%M%S"), os.getpid())
        self._env = None
        self._mode = None

    def _restart(self, **kw):
        self.close()
        self._env = AuditEnv(run_id=self.run_id, runs_dir=self.runs_dir, **kw)

    def reset(self, seed: int = 0, config: dict | None = None):
        """config: {"mode": "closed"|"train", "episode_dir": str (необязательно), "team_config": {...}}."""
        config = config or {"mode": "train"}
        if not isinstance(config, dict):
            raise ValueError("config должен быть объектом")
        mode = _env_mode(config.get("mode", "train"))
        team = dict(config.get("team_config") or {})
        episode_dir = config.get("episode_dir")
        server_cfg = {"mode": mode}
        if episode_dir:
            # закрытый эпизод жюри: грузим именно его, ID не меняем, отпечаток пересчитываем
            if "config_version" in team:
                server_cfg["config_version"] = team["config_version"]
            self._restart(episode=episode_dir, trust_episode=True)
        else:
            # открытый режим: генератор команды с переданными seed и team_config
            server_cfg = dict(team, mode=mode)
            self._restart()
        obs, info = self._env.reset(seed, server_cfg)
        self._mode = "closed" if mode == "eval" else "train"
        first = {"task": obs["task"], "manifest": obs["manifest"], "policy": obs["policy"],
                 "remaining_steps": obs["steps_remaining"]}
        return first, {"episode_id": info["episode_id"], "mode": self._mode}

    def step(self, action):
        """action: {"name": <действие>, "args": {...}} -> (observation, reward, terminated, truncated, info)."""
        if self._env is None:
            raise EnvError("NO_EPISODE", "Эпизод не начат: сначала вызовите reset.")
        if isinstance(action, dict) and set(action) == {"name", "args"}:
            action = {"tool": action["name"], "args": action["args"]}
        # всё остальное уходит как есть: среда ответит INVALID_ACTION и израсходует шаг
        obs, reward, term, trunc, info = self._env.step(action)
        # в info только публичное: ответы оценщика (metrics) политике не передаются
        return (_translate(obs), float(reward), bool(term), bool(trunc),
                {"episode_id": info["episode_id"], "mode": self._mode})

    def close(self):
        if self._env is not None:
            self._env.close()
            self._env = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def make_env() -> JudgeEnv:
    return JudgeEnv()


# ------------------------------------------------------------------ оценка

def score_episode(episode_dir: str, findings: dict, inspection_steps: int) -> dict:
    """Закрытый вызов оценщика: {"tp","fp","fn","precision","recall","f1","return"}.
    Невалидный submit: return=-1, остальные поля null, ошибка пишется в журнал оценщика."""
    from env.episode import load_public, load_truth
    from env.fingerprint import episode_fingerprint, public_hashes
    from grader.evaluate import evaluate
    ep = Path(episode_dir)
    public = load_public(ep)
    episode_id = public.manifest["episode_id"]
    # доказательство, которого нет в открытых данных, не сопоставляется (соглашение жюри, M04)
    res = evaluate(findings, load_truth(ep), episode_id, inspection_steps,
                   public_ids=public.public_ids())
    _log_score(episode_id, inspection_steps, res, episode_fingerprint(public_hashes(ep)))
    if not res["valid"]:
        return {"tp": None, "fp": None, "fn": None, "precision": None, "recall": None,
                "f1": None, "return": -1.0}
    m = res["metrics"]
    return {"tp": m["tp"], "fp": m["fp"], "fn": m["fn"], "precision": m["precision"],
            "recall": m["recall"], "f1": m["f1"], "return": float(res["reward"])}


def _log_score(episode_id, inspection_steps, res, fingerprint):
    """Строка журнала оценщика для score_episode (runs/judge-scores/score_log.jsonl)."""
    row = {"episode_id": episode_id, "inspection_steps": inspection_steps, "valid": res["valid"],
           "reward": res["reward"], "error": res["error"], "metrics": res["metrics"],
           "episode_sha256": fingerprint}
    try:
        path = _runs_dir() / "judge-scores" / "score_log.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass  # журнал не должен ломать оценку на файловой системе только для чтения


# ------------------------------------------------------------------ самопроверка

def main() -> int:
    """Генерация -> закрытый эпизод -> чтение ledger -> пустой submit -> score_episode."""
    with tempfile.TemporaryDirectory() as d:
        ep = Path(d) / "episode"
        generate_episode(1, {}, str(ep))
        with make_env() as env:
            obs, info = env.reset(0, {"mode": "closed", "episode_dir": str(ep), "team_config": {}})
            steps = 0
            out, *_ = env.step({"name": "list_records", "args": {"table": "ledger", "cursor": None,
                                                                 "limit": 50}})
            steps += 1
            fin = {"episode_id": info["episode_id"], "findings": []}
            last, reward, term, trunc, _ = env.step({"name": "submit", "args": {"findings": fin}})
        score = score_episode(str(ep), fin, steps)
    print(json.dumps({"episode_id": info["episode_id"], "first_page": len(out["result"]["records"]),
                      "submit": last, "reward_to_policy": reward, "terminated": term,
                      "score": score}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
