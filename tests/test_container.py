"""Проверки изоляции контейнера. Работают только внутри `python -m scripts.check --docker`.

Вне контейнера (локально и в обычном CI) пропускаются: их включает переменная
AUDIT_GYM_EXPECT_ISOLATION=1, которую выставляет scripts.check --docker.
"""
import os
import socket
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("AUDIT_GYM_EXPECT_ISOLATION") != "1",
                                reason="только внутри контейнера (scripts.check --docker)")

ROOT = Path(__file__).resolve().parent.parent


def test_no_network_ip():
    """Нет сети: соединение по IP невозможно."""
    with pytest.raises(OSError):
        socket.create_connection(("1.1.1.1", 53), timeout=2).close()


def test_no_dns():
    """Нет сети: имена не разрешаются."""
    with pytest.raises(OSError):
        socket.getaddrinfo("pypi.org", 443)


def test_not_root():
    """Проверка идёт не от root."""
    assert os.geteuid() != 0


def test_code_is_read_only():
    """Код и эпизоды нельзя изменить; писать можно только во временную папку."""
    with pytest.raises(OSError):
        (ROOT / "should_not_exist.txt").write_text("x", encoding="utf-8")
    with pytest.raises(OSError):
        (ROOT / "env" / "core.py").open("a", encoding="utf-8").close()
    probe = Path("/tmp") / "audit_gym_probe.txt"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()


def test_python_version():
    """Внутри образа ровно Python 3.11, как требует кейс."""
    import sys
    assert sys.version_info[:2] == (3, 11)
