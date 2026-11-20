"""Детерминизм, отпечаток эпизода, статическая проверка запрещённых источников недетерминизма."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from common.jsonio import canonical_dumps
from env.client import AuditEnv, EnvError
from env.fingerprint import EpisodeIntegrityError, PUBLIC_FILES, public_hashes, verify_episode

ROOT = Path(__file__).resolve().parent.parent
EP_DIR = ROOT / "tests" / "fixtures" / "mini" / "EP-8K2M4Q7X"
GRADER = "tests.fake_grader:evaluate"
SEEDS = (0, 1, 7, 42, 2026)


# ---------- вспомогательное: настоящий генератор ----------
def _config():
    """DEFAULT_CONFIG генератора или пропуск, если модуля ещё нет."""
    mod = pytest.importorskip("generator.config")
    return mod.DEFAULT_CONFIG


def _generate(seed, out_dir):
    """Генерирует эпизод; без реализации генератора тест пропускается."""
    config = _config()
    from generator.build import generate_episode
    try:
        return Path(generate_episode(seed, config, Path(out_dir)))
    except NotImplementedError:
        pytest.skip("генератор A не реализован")


def _gen_json(ep):
    return json.loads((ep / "private" / "generation.json").read_text(encoding="utf-8"))


@pytest.fixture
def tmp():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


# ---------- a) тот же seed -> те же хэши ----------
@pytest.mark.parametrize("seed", SEEDS)
def test_same_seed_same_hashes(tmp, seed):
    a = _generate(seed, tmp / "a")
    b = _generate(seed, tmp / "b")
    ga, gb = _gen_json(a), _gen_json(b)
    assert ga["public_sha256"] == gb["public_sha256"]
    assert ga["episode_sha256"] == gb["episode_sha256"]
    assert (a / "private" / "truth.json").read_bytes() == (b / "private" / "truth.json").read_bytes()


# ---------- b) разные процессы и PYTHONHASHSEED ----------
_SCRIPT = """
import sys, tempfile
from pathlib import Path
from generator.build import generate_episode
from generator.config import DEFAULT_CONFIG
from env.fingerprint import verify_episode
with tempfile.TemporaryDirectory() as d:
    ep = generate_episode(int(sys.argv[1]), DEFAULT_CONFIG, Path(d))
    print(verify_episode(ep))
"""


def _fingerprint_in_subprocess(seed, hashseed):
    env = dict(os.environ, PYTHONHASHSEED=str(hashseed))
    p = subprocess.run([sys.executable, "-c", _SCRIPT, str(seed)], cwd=str(ROOT), env=env,
                       capture_output=True, text=True, timeout=300)
    if "NotImplementedError" in p.stderr:
        pytest.skip("генератор A не реализован")
    assert p.returncode == 0, p.stderr[-500:]
    return p.stdout.strip()


def test_hashseed_independent():
    _config()
    results = {_fingerprint_in_subprocess(42, hs) for hs in (0, 12345)}
    assert len(results) == 1


# ---------- c) разные seed -> разные отпечатки ----------
def test_different_seeds_differ(tmp):
    a = _generate(1, tmp / "a")
    b = _generate(2, tmp / "b")
    assert _gen_json(a)["episode_sha256"] != _gen_json(b)["episode_sha256"]


# ---------- d) verify_episode на сгенерированном эпизоде ----------
def test_verify_generated_and_tamper(tmp):
    ep = _generate(7, tmp / "g")
    assert verify_episode(ep) == _gen_json(ep)["episode_sha256"]
    ledger = ep / "public" / "ledger.csv"
    original = ledger.read_bytes()
    data = bytearray(original)
    data[-2] = (data[-2] + 1) % 256  # меняем один байт
    ledger.write_bytes(bytes(data))
    with pytest.raises(EpisodeIntegrityError):
        verify_episode(ep)
    ledger.write_bytes(original)
    assert verify_episode(ep)  # вернули байт - снова проходит
    (ep / "public" / "fx.csv").unlink()
    with pytest.raises(EpisodeIntegrityError):
        verify_episode(ep)


# ---------- e) сервер: два независимых процесса дают одинаковые ответы ----------
def _session(runs_dir, run_id, seed):
    """Прогоняет фиксированную последовательность действий, возвращает ответы и журнал."""
    cfg = dict(_config(), mode="train")
    out = []
    with AuditEnv(grader=GRADER, run_id=run_id, runs_dir=runs_dir) as env:
        try:
            obs, info = env.reset(seed, cfg)
        except EnvError as e:
            if e.code == "GENERATION_FAILED":
                pytest.skip("генератор A не реализован")
            raise
        out.append((obs, info))
        r1 = env.step({"tool": "list_records", "args": {"table": "ledger"}})
        out.append(r1)
        res = r1[0]["result"]
        first = res["records"][0]["row_id"]
        if res["next_cursor"]:
            out.append(env.step({"tool": "list_records",
                                 "args": {"table": "ledger", "cursor": res["next_cursor"]}}))
        out.append(env.step({"tool": "get_record", "args": {"table": "ledger", "id": first}}))
        out.append(env.step({"tool": "calculator", "args": {"expression": "1+2"}}))
        out.append(env.step({"tool": "submit", "args": {"findings": {
            "episode_id": info["episode_id"], "findings": []}}}))
    log = Path(runs_dir) / run_id / "grader_log.jsonl"
    return out, [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]


def test_server_same_seed_same_responses(tmp):
    a, log_a = _session(tmp / "runs", "ra", 42)
    b, log_b = _session(tmp / "runs", "rb", 42)
    assert canonical_dumps(a) == canonical_dumps(b)
    fp_a, fp_b = log_a[0]["episode_sha256"], log_b[0]["episode_sha256"]
    assert fp_a == fp_b
    # то же значение даёт отдельная генерация
    assert fp_a == _gen_json(_generate(42, tmp / "g"))["episode_sha256"]


# ---------- f) без генератора: фикстура и повреждённая копия ----------
def test_fixture_verifies():
    gen = json.loads((EP_DIR / "private" / "generation.json").read_text(encoding="utf-8"))
    assert verify_episode(EP_DIR) == gen["episode_sha256"]
    assert set(public_hashes(EP_DIR)) == set(PUBLIC_FILES)


def test_server_rejects_tampered_episode(tmp):
    copy = tmp / "EP"
    shutil.copytree(EP_DIR, copy)
    ledger = copy / "public" / "ledger.csv"
    data = bytearray(ledger.read_bytes())
    data[-2] = (data[-2] + 1) % 256
    ledger.write_bytes(bytes(data))
    with AuditEnv(episode=copy, grader=GRADER, run_id="r", runs_dir=tmp / "runs") as env:
        with pytest.raises(EnvError) as ei:
            env.reset(1, {"mode": "train"})
        assert ei.value.code == "EPISODE_INVALID"
        assert str(copy) not in ei.value.message
        # сервер жив: ошибка протокола, а не SERVER_DIED
        with pytest.raises(EnvError) as ei2:
            env.step({"tool": "calculator", "args": {"expression": "1"}})
        assert ei2.value.code == "NO_EPISODE"


def test_server_logs_fingerprint(tmp):
    with AuditEnv(episode=EP_DIR, grader=GRADER, run_id="r", runs_dir=tmp / "runs") as env:
        _, info = env.reset(1, {"mode": "train"})
        obs, _, _, _, info2 = env.step({"tool": "submit", "args": {"findings": {
            "episode_id": "EP-8K2M4Q7X", "findings": []}}})
    line = (tmp / "runs" / "r" / "grader_log.jsonl").read_text(encoding="utf-8").splitlines()[0]
    row = json.loads(line)
    assert row["episode_sha256"] == verify_episode(EP_DIR)
    # агенту отпечаток не передаётся
    assert row["episode_sha256"] not in canonical_dumps([info, obs, info2])


# ---------- g) статическая проверка кода ----------
_FORBIDDEN = [
    r"datetime\.now\(", r"datetime\.utcnow\(", r"date\.today\(", r"time\.time\(",
    r"(?<![\w.])hash\(",
    r"random\.(random|randint|randrange|choice|choices|shuffle|sample|uniform|seed)\(",
    r"uuid\.uuid[14]\(", r"os\.urandom", r"secrets\.",
]
_FORBIDDEN_RE = re.compile("|".join("(?:%s)" % p for p in _FORBIDDEN))


def test_no_nondeterministic_calls():
    bad = []
    for pkg in ("env", "generator", "grader", "common"):
        for path in sorted((ROOT / pkg).rglob("*.py")):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "# determinism: ok" in line:
                    continue
                if _FORBIDDEN_RE.search(line):
                    bad.append("%s:%d: %s" % (path.relative_to(ROOT).as_posix(), n, line.strip()))
    assert not bad, "Недетерминированные вызовы:\n" + "\n".join(bad)
