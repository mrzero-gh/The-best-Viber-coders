"""Клиент среды в процессе агента: общается с env.server через subprocess и JSON-строки."""
import json
import subprocess
import sys
from pathlib import Path

from common.jsonio import canonical_dumps

_ROOT = Path(__file__).resolve().parent.parent


class EnvError(Exception):
    """Ошибка протокола от сервера (или SERVER_DIED, если процесс умер)."""

    def __init__(self, code: str, message: str):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message


class AuditEnv:
    """Клиент в процессе агента. Ничего не хранит, кроме процесса сервера."""

    def __init__(self, *, run_id=None, runs_dir=None, episode=None, grader=None, time_limit_s=None,
                 trust_episode=False):
        cmd = [sys.executable, "-m", "env.server"]
        if run_id is not None:
            cmd += ["--run-id", str(run_id)]
        if runs_dir is not None:
            cmd += ["--runs-dir", str(runs_dir)]
        if episode is not None:
            cmd += ["--episode", str(episode)]
        if trust_episode:
            cmd += ["--trust-episode"]
        if grader is not None:
            cmd += ["--grader", grader]
        if time_limit_s is not None:
            cmd += ["--time-limit-s", repr(float(time_limit_s))]
        # бинарные пайпы: кодируем UTF-8 вручную, чтобы на Windows не было \r\n
        self._proc = subprocess.Popen(cmd, cwd=str(_ROOT), stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE)
        self._closed = False

    def _call(self, msg: dict) -> dict:
        try:
            self._proc.stdin.write((canonical_dumps(msg) + "\n").encode("utf-8"))
            self._proc.stdin.flush()
            line = self._proc.stdout.readline()
        except (BrokenPipeError, OSError):
            line = b""
        if not line:
            raise EnvError("SERVER_DIED", "Процесс среды завершился.")
        resp = json.loads(line.decode("utf-8"))
        if "error" in resp:
            raise EnvError(resp["error"]["code"], resp["error"]["message"])
        return resp

    def reset(self, seed: int = 0, config: dict | None = None, *, options: dict | None = None) -> tuple[dict, dict]:
        """Начинает эпизод; возвращает (observation, info)."""
        if config is None:
            if options and isinstance(options, dict) and "config" in options:
                config = options["config"]
            else:
                config = {"mode": "train"}
        r = self._call({"cmd": "reset", "seed": seed, "config": config})
        return r["observation"], r["info"]

    def step(self, action: dict) -> tuple[dict, float, bool, bool, dict]:
        """Один шаг; возвращает (observation, reward, terminated, truncated, info)."""
        r = self._call({"cmd": "step", "action": action})
        return r["observation"], r["reward"], r["terminated"], r["truncated"], r["info"]

    def close(self) -> None:
        """Завершает серверный процесс (идемпотентно)."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._proc.poll() is None:
                try:
                    self._call({"cmd": "close"})
                except EnvError:
                    pass
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        finally:
            for f in (self._proc.stdin, self._proc.stdout):
                try:
                    f.close()
                except OSError:
                    pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
