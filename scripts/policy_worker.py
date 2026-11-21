"""Процесс политики для scripts.jury: python -m scripts.policy_worker <политика>.

Политика работает в отдельном процессе и видит только то, что ей присылают: наблюдения среды.
Эталона, путей к эпизоду и памяти запускателя у неё нет. Эталон получает только OraclePolicy,
и только явной командой: это эталонная политика по определению.

Протокол - JSON-строки UTF-8 через stdin/stdout:
  {"cmd": "reset"}                     -> {"ok": true}
  {"cmd": "truth", "truth": {...}}     -> {"ok": true}  (только у политики с set_truth)
  {"cmd": "act", "observation": {...}} -> {"action": {...}}
Конец stdin - выход. Вывод самой политики в stdout уходит в stderr и протокол не ломает.
<политика> - имя из POLICIES или "модуль:класс" (для тестов изоляции).
"""
import importlib
import json
import sys

POLICIES = {
    "OraclePolicy": lambda: _policies().OraclePolicy(),
    "SmartAuditor": lambda: _policies().RuleBasedPolicy(naive=False),
    "SimpleRule": lambda: _policies().SimpleRulePolicy(),
    "FlagAllPolicy": lambda: _policies().FlagAllPolicy(),
    "RandomPolicy": lambda: _policies().RandomPolicy(seed=42),
}


def _policies():
    return importlib.import_module("policies")


def make_policy(spec: str):
    if spec in POLICIES:
        return POLICIES[spec]()
    module, _, attr = spec.partition(":")
    if not module or not attr:
        raise SystemExit(f"неизвестная политика: {spec}")
    return getattr(importlib.import_module(module), attr)()


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        raise SystemExit("использование: python -m scripts.policy_worker <политика>")
    out = sys.stdout.buffer
    sys.stdout = sys.stderr  # print() политики не попадает в протокол
    policy = make_policy(argv[0])
    for line in sys.stdin.buffer:
        msg = json.loads(line.decode("utf-8"))
        cmd = msg.get("cmd")
        if cmd == "reset":
            policy.reset()
            reply = {"ok": True}
        elif cmd == "truth":
            if not hasattr(policy, "set_truth"):
                raise SystemExit("эталон передаётся только OraclePolicy")
            policy.set_truth(msg["truth"])
            reply = {"ok": True}
        elif cmd == "act":
            reply = {"action": policy.act(msg["observation"])}
        else:
            raise SystemExit(f"неизвестная команда: {cmd}")
        out.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
        out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
