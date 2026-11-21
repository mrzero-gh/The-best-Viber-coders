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
import os
import sys

# Чего процесс политики не может делать (Python audit hook, ставится до импорта политики и не
# снимается): читать закрытые файлы эпизода и журналы оценщика, писать файлы, импортировать оценщик,
# генератор и сервер среды, запускать процессы, грузить нативные библиотеки (ctypes), открывать сеть.
_SECRET_NAMES = {"truth.json", "provenance.json", "generation.json", "certificate.json", "grader_log.jsonl",
                 "score_log.jsonl"}
_SECRET_DIRS = {"private", "runs"}
_BLOCKED_MODULES = {"grader", "generator", "judge_adapter", "ctypes", "_ctypes", "socket", "_socket",
                    "subprocess", "_posixsubprocess", "_winapi", "multiprocessing"}
_BLOCKED_ENV_MODULES = {"env.server", "env.episode", "env.core", "env.client", "env.fingerprint"}
_BLOCKED_EVENTS = {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.startfile",
                   "os.fork", "os.forkpty", "ctypes.dlopen", "ctypes.dlsym", "ctypes.cdata", "socket.connect",
                   "socket.bind", "socket.getaddrinfo", "winreg.OpenKey", "_winapi.CreateProcess",
                   "os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.chmod", "shutil.rmtree"}
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def _denied(path) -> bool:
    if isinstance(path, int):  # уже открытый дескриптор (stdin/stdout)
        return False
    text = os.fsdecode(os.fspath(path))
    parts = [part.lower() for part in os.path.abspath(text).replace("\\", "/").split("/")]
    return bool(parts) and (parts[-1] in _SECRET_NAMES or bool(_SECRET_DIRS & set(parts[:-1])))


def _audit(event: str, args: tuple) -> None:
    if event == "open":
        path, mode, flags = (tuple(args) + (None, None))[:3]
        writing = (isinstance(mode, str) and any(ch in mode for ch in "wax+")) or (
            isinstance(flags, int) and flags & _WRITE_FLAGS)
        if path is not None and (_denied(path) or (writing and not isinstance(path, int))):
            raise PermissionError("policy process: file access denied")
    elif event == "import":
        name = str(args[0])
        if name.split(".")[0] in _BLOCKED_MODULES or name in _BLOCKED_ENV_MODULES:
            raise ImportError(f"policy process: import of {name} is not allowed")
    elif event in _BLOCKED_EVENTS or event.startswith(("subprocess.", "socket.", "ctypes.")):
        raise PermissionError(f"policy process: {event} is not allowed")


def install_guard() -> None:
    """Ограничения процесса политики; снять audit hook из Python нельзя."""
    sys.addaudithook(_audit)


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
    import policies  # noqa: F401 - пакет базовых политик грузится до ограничений
    install_guard()
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
