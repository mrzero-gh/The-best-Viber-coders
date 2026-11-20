"""Единая команда проверки.

    python -m scripts.check            # pytest в текущем окружении
    python -m scripts.check --docker   # сборка образа и pytest в контейнере без сети

В режиме --docker контейнер запускается как у жюри: без сети, файловая система только для
чтения (запись только в /tmp), не от root, 2 CPU и 4 ГБ памяти.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "audit-gym"
# Ограничения как в кейсе: 2 CPU, 4 ГБ, без сети
RUN_FLAGS = [
    "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,size=512m",
    "--cpus", "2", "--memory", "4g",
    "-e", "AUDIT_GYM_EXPECT_ISOLATION=1",
]
_WIN_DOCKER = r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"


def _docker() -> str:
    """Путь к docker: из PATH или стандартный путь Docker Desktop на Windows."""
    found = shutil.which("docker")
    if found:
        return found
    if os.name == "nt" and Path(_WIN_DOCKER).exists():
        return _WIN_DOCKER
    raise SystemExit("docker не найден: установите Docker Desktop и запустите его")


def run_local() -> int:
    """pytest в текущем окружении.

    Временные папки тестов - в собственной новой папке (--basetemp): общая системная
    pytest-of-<user> на некоторых Windows-машинах недоступна (PermissionError).
    """
    base = tempfile.mkdtemp(prefix="audit-gym-pytest-")
    try:
        return subprocess.call([sys.executable, "-m", "pytest", "-q", "--basetemp", base])
    finally:
        shutil.rmtree(base, ignore_errors=True)


def run_docker() -> int:
    """Сборка образа и проверка внутри контейнера без сети."""
    docker = _docker()
    code = subprocess.call([docker, "build", "-t", IMAGE, "."], cwd=str(ROOT))
    if code != 0:
        print("Сборка образа не удалась")
        return code
    cmd = [docker, "run", *RUN_FLAGS, IMAGE]
    print("Запуск:", " ".join(["docker"] + cmd[1:]))
    return subprocess.call(cmd, cwd=str(ROOT))


def main(argv=None) -> int:
    """Запускает проверку, печатает итог и возвращает код возврата."""
    ap = argparse.ArgumentParser(prog="scripts.check")
    ap.add_argument("--docker", action="store_true", help="проверка в контейнере без сети")
    args = ap.parse_args(argv)
    code = run_docker() if args.docker else run_local()
    print("CHECK OK" if code == 0 else "CHECK FAILED (код %d)" % code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
