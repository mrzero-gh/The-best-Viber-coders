"""Потоки случайности и непрозрачные ID (контракт, разделы 3 и 15)."""
import hashlib
import random

# base32 Крокфорда: без I, L, O, U
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def stream(seed, name: str) -> random.Random:
    """Независимый поток RNG: sha256(f"{seed}:{name}") -> первые 8 байт."""
    digest = hashlib.sha256(f"{seed}:{name}".encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


class IdFactory:
    """Выдаёт уникальные ID вида ПРЕФИКС-XXXXXXXX из своего потока.

    ID берутся в порядке создания объектов, а этот порядок задаёт только содержание.
    Поэтому смена потока ids меняет значения ID, но не содержание эпизода.
    """

    def __init__(self, rng: random.Random):
        self._rng = rng
        self._used = set()

    def new(self, prefix: str) -> str:
        while True:
            value = prefix + "-" + "".join(self._rng.choice(CROCKFORD) for _ in range(8))
            if value not in self._used:
                self._used.add(value)
                return value
