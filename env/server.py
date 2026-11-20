"""Серверный процесс среды: `python -m env.server`. Агент не имеет доступа к private/.

Протокол: одна JSON-строка на команду и на ответ (UTF-8, перевод строки LF), см. docs/contract.md, раздел 2.
"""
import argparse
import importlib
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

from common.jsonio import canonical_dumps
from env.core import AuditCore
from env.episode import load_public, load_truth
from env.fingerprint import EpisodeIntegrityError, verify_episode

MAX_LINE = 1024 * 1024  # лимит длины строки команды, байт

# Фиксированные тексты ошибок протокола (без путей и деталей)
_ERR = {
    "BAD_REQUEST": "Некорректный запрос.",
    "BAD_CONFIG": "Некорректный config: нужен объект, mode - train или eval.",
    "NO_EPISODE": "Эпизод не начат: сначала вызовите reset.",
    "GENERATION_FAILED": "Не удалось сгенерировать эпизод.",
    "EPISODE_INVALID": "Эпизод повреждён: проверка отпечатка не пройдена.",
    "INTERNAL": "Внутренняя ошибка сервера.",
}


class _ProtoError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _load_grader(spec):
    """Импортирует функцию оценщика из строки module:func."""
    mod, _, func = spec.partition(":")
    return getattr(importlib.import_module(mod), func)


class Server:
    def __init__(self, args, out):
        self.args = args
        self.out = out
        self.grader = _load_grader(args.grader) if args.grader else None
        self.core = None
        self.mode = "train"
        self.seed = None
        self.config_version = None
        self.episode_sha256 = None
        self.logged = False
        self.log_path = Path(args.runs_dir) / args.run_id / "grader_log.jsonl"

    # ---------- вывод ----------
    def send(self, obj):
        self.out.write((canonical_dumps(obj) + "\n").encode("utf-8"))
        self.out.flush()

    def error(self, code):
        self.send({"error": {"code": code, "message": _ERR[code]}})

    # ---------- эпизод ----------
    def _new_episode(self, seed, config):
        mode = config["mode"]
        try:
            if self.args.episode:
                ep_dir = Path(self.args.episode)
                fingerprint = verify_episode(ep_dir)
                episode, truth = load_public(ep_dir), load_truth(ep_dir)
            else:
                tmp = tempfile.mkdtemp(prefix="auditgym-")
                try:
                    try:
                        from generator.build import generate_episode
                        ep_dir = generate_episode(seed, config, Path(tmp))
                        # проверка отпечатка и загрузка - ДО удаления временной папки
                        fingerprint = verify_episode(ep_dir)
                        episode, truth = load_public(ep_dir), load_truth(ep_dir)
                    except EpisodeIntegrityError:
                        raise
                    except Exception:
                        if mode == "train":
                            traceback.print_exc(file=sys.stderr)
                        raise _ProtoError("GENERATION_FAILED")
                finally:
                    # truth живёт на диске доли секунды: удаляем всю папку сразу после загрузки
                    shutil.rmtree(tmp, ignore_errors=True)
        except EpisodeIntegrityError:
            raise _ProtoError("EPISODE_INVALID")
        kw = {}
        if self.args.time_limit_s is not None:
            kw["time_limit_s"] = self.args.time_limit_s
        self.core = AuditCore(episode, truth, mode=mode, grader=self.grader, **kw)
        self.mode = mode
        self.episode_sha256 = fingerprint
        self.seed = None if self.args.episode else seed
        cv = config.get("config_version")
        self.config_version = cv if isinstance(cv, (str, int)) and not isinstance(cv, bool) else None
        self.logged = False

    def _write_log(self, status):
        """Одна строка журнала оценщика при завершении эпизода (в обоих режимах)."""
        core, res = self.core, self.core.grader_result
        row = {"run_id": self.args.run_id, "episode_id": core.episode_id, "mode": self.mode,
               "seed": self.seed, "config_version": self.config_version, "status": status,
               "steps_used": core.steps_used, "inspection_steps": core.inspection_steps,
               "reward": res.get("reward"), "valid": res.get("valid"),
               "error": res.get("error"), "metrics": res.get("metrics"),
               "episode_sha256": self.episode_sha256}
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(canonical_dumps(row) + "\n")
        self.logged = True

    # ---------- команды ----------
    def handle(self, line):
        """Обрабатывает команду; False - нужно завершить работу."""
        try:
            msg = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise _ProtoError("BAD_REQUEST")
        if not isinstance(msg, dict) or not isinstance(msg.get("cmd"), str):
            raise _ProtoError("BAD_REQUEST")
        cmd = msg["cmd"]
        if cmd == "reset":
            if set(msg) != {"cmd", "seed", "config"}:
                raise _ProtoError("BAD_REQUEST")
            seed, config = msg["seed"], msg["config"]
            if not isinstance(seed, int) or isinstance(seed, bool):
                raise _ProtoError("BAD_REQUEST")
            if not isinstance(config, dict) or config.get("mode") not in ("train", "eval"):
                raise _ProtoError("BAD_CONFIG")
            self.core = None
            self._new_episode(seed, config)
            self.send({"observation": self.core.initial_observation(),
                       "info": {"episode_id": self.core.episode_id, "mode": self.mode}})
        elif cmd == "step":
            if set(msg) != {"cmd", "action"}:
                raise _ProtoError("BAD_REQUEST")
            if self.core is None:
                raise _ProtoError("NO_EPISODE")
            obs, reward, term, trunc, info = self.core.step(msg["action"])
            if self.core.finished and not self.logged:
                self._write_log(obs["status"])
            self.send({"observation": obs, "reward": reward, "terminated": term,
                       "truncated": trunc, "info": info})
        elif cmd == "close":
            if set(msg) != {"cmd"}:
                raise _ProtoError("BAD_REQUEST")
            self.send({"ok": True})
            return False
        else:
            raise _ProtoError("BAD_REQUEST")
        return True

    def loop(self, inp):
        while True:
            line = inp.readline(MAX_LINE + 1)
            if not line:
                return 0  # EOF
            if len(line) > MAX_LINE and not line.endswith(b"\n"):
                # слишком длинная строка: дочитываем до \n и отвечаем ошибкой
                while line and not line.endswith(b"\n"):
                    line = inp.readline(MAX_LINE)
                self.error("BAD_REQUEST")
                if not line:
                    return 0
                continue
            try:
                if not self.handle(line):
                    return 0
            except _ProtoError as e:
                self.error(e.code)
            except Exception:
                if self.mode == "train":
                    traceback.print_exc(file=sys.stderr)
                self.error("INTERNAL")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="env.server")
    ap.add_argument("--run-id", default="run-%s-%d" % (time.strftime("%Y%m%d-%H%M%S"), os.getpid()))
    # По умолчанию runs/; в контейнере с файловой системой только для чтения — AUDIT_GYM_RUNS_DIR=/tmp/runs
    ap.add_argument("--runs-dir", default=os.environ.get("AUDIT_GYM_RUNS_DIR") or "runs")
    ap.add_argument("--episode", default=None)
    ap.add_argument("--grader", default=None)  # только для тестов
    ap.add_argument("--time-limit-s", type=float, default=None)  # только для тестов
    args = ap.parse_args(argv)
    # Настоящий stdout (fd 1) - только для протокола: копируем его в отдельный дескриптор,
    # а fd 1 и sys.stdout направляем в stderr. Так случайный вывод (print, C-расширения,
    # дочерние процессы) не ломает протокол.
    sys.stdout.flush()
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return Server(args, out).loop(sys.stdin.buffer)


if __name__ == "__main__":
    raise SystemExit(main())
