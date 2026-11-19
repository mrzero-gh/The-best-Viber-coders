"""Единая команда проверки: python -m scripts.check."""
import subprocess
import sys


def main() -> int:
    """Запускает pytest, печатает итог и возвращает код возврата."""
    code = subprocess.call([sys.executable, "-m", "pytest", "-q"])
    print("CHECK OK" if code == 0 else "CHECK FAILED (код %d)" % code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
