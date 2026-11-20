"""Канарейки и аудит утечек (задача #20): в выходе среды нет меток эталона и путей к файлам."""
import copy
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from common.jsonio import canonical_dumps
from env import leak_audit as la
from env.client import AuditEnv
from env.fingerprint import verify_episode
from generator.build import generate_episode
from generator.config import DEFAULT_CONFIG, INTERACTIONS
from grader.validation import validate_truth

ROOT = Path(__file__).resolve().parent.parent


def _configs():
    inter = copy.deepcopy(DEFAULT_CONFIG)
    inter["interactions"] = {s: {"enabled": True, "count": [1, 1]} for s in INTERACTIONS}
    return {"default": copy.deepcopy(DEFAULT_CONFIG), "interactions": inter}


CONFIGS = _configs()


@pytest.fixture
def tmp():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


# ---------- сам сканер ----------
def test_scan_finds_markers_and_escaped_paths():
    # слова без учёта регистра, ID и хэши точно, пути в JSON-экранировании
    assert la.scan(["xx TrUtH yy"], {"truth"})
    assert la.scan(["ISS-0123ABCD"], {"ISS-0123ABCD"})
    assert not la.scan(["iss-0123abcd"], {"ISS-0123ABCD"})
    text = canonical_dumps({"m": 'C:\\Users\\x\\a.py'})
    assert la.scan([text], la.path_markers())
    assert la.scan(["Traceback (most recent call last)"], la.path_markers())
    hit = la.scan(["a" * 200 + "SECRET" + "b" * 200], {"secret"})
    assert hit and len(hit[0][1]) <= 80


def test_markers_exclude_public_ids(tmp):
    ep = generate_episode(1, CONFIGS["interactions"], tmp)
    m = la.private_markers(ep)
    pub = la._public_strings(ep)
    assert m and not (m & pub), "публичные значения не должны быть метками"
    assert "issue_id" in m and any(x.startswith("ISS-") for x in m)


# ---------- основной аудит: 3 seed x 2 конфига x {train, eval} ----------
@pytest.mark.parametrize("name", list(CONFIGS))
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_no_leaks(seed, name, tmp):
    report = la.run_audit([seed], {name: CONFIGS[name]}, tmp / "runs")
    assert report["cells"] == 2 and report["responses"] > 200
    assert report["leaks"] == [], report["leaks"][:3]
    assert report["violations"] == [], report["violations"][:3]


# ---------- канарейки ----------
def test_canaries_never_reach_output(tmp):
    cfg = CONFIGS["interactions"]
    src = generate_episode(5, cfg, tmp / "src")
    canary_ep, canaries = la.make_canary_episode(src, tmp / "canary")
    # отпечаток сходится, truth по схеме валиден
    verify_episode(canary_ep)
    truth = la.load_truth(canary_ep)
    validate_truth(truth, truth["episode_id"])
    assert truth["issues"], "в эпизоде нет ошибок: канарейки бессмысленны"
    assert any(c.startswith("ISS-CANARY") for c in canaries) and la.CANARY_GEN in canaries
    assert la.CANARY_PROV in (canary_ep / "private" / "provenance.json").read_text(encoding="utf-8")
    markers = la.private_markers(canary_ep)
    for c in canaries:
        assert c in markers or "CANARY" in c
    for mode in ("train", "eval"):
        sink = la.run_cell(lambda: AuditEnv(episode=canary_ep, runs_dir=tmp / "runs", run_id="c-" + mode),
                           mode, 5, cfg, truth, generated=False)
        assert sink.violations == []
        # ответы, сообщения EnvError и строки траектории
        everything = [t for _s, t in sink.texts] + sink.trajectory
        assert len(sink.trajectory) > 100
        assert la.scan(everything, {"canary"}) == []
        assert la.scan(everything, markers | la.path_markers(tmp, canary_ep)) == []


def test_canary_scan_detects_planted_leak(tmp):
    """Контроль самого аудита: подложенная метка находится."""
    assert la.scan(['{"error":{"message":"ISS-CANARY01 в ответе"}}'], {"ISS-CANARY01"})
    assert la.scan(["x CANARY-GENERATION-7Q4Z"], {"canary"})


# ---------- временные папки ----------
def test_no_leftover_temp_dirs(tmp):
    before = la._temp_dirs()
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["mode"] = "eval"
    with AuditEnv(runs_dir=tmp / "runs") as env:
        env.reset(1, cfg)
        assert la._temp_dirs() - before == set(), "после reset осталась папка auditgym-*"
        env.reset(2, cfg)
        assert la._temp_dirs() - before == set()
    assert la._temp_dirs() - before == set(), "после close осталась папка auditgym-*"


# ---------- stderr в eval ----------
def test_eval_stderr_is_empty(tmp):
    cfg = {"mode": "eval"}
    bad_gen = {"mode": "eval", "size": {"ledger_rows": [10 ** 9, 1]}}
    lines = [
        {"cmd": "reset", "seed": 1, "config": cfg},
        {"cmd": "step", "action": {"tool": "nope", "args": {}}},
        {"cmd": "step", "action": {"tool": "get_record", "args": {"table": "ledger", "id": "L-" + "X" * 500}}},
        {"cmd": "step", "action": {"tool": "calculator", "args": {"expression": "__import__('os')"}}},
        {"cmd": "step", "action": "мусор"},
        {"cmd": "reset", "seed": 1, "config": bad_gen},
        {"cmd": "step", "action": {"tool": "calculator", "args": {"expression": "1"}}},
        {"cmd": "reset", "seed": "x", "config": cfg},
        {"cmd": "close"},
    ]
    data = b"".join((canonical_dumps(m) + "\n").encode("utf-8") for m in lines)
    data = data[:data.index(b'{"cmd":"close"}')] + b"not json\n" + b'{"cmd":"close"}\n'
    proc = subprocess.run([sys.executable, "-m", "env.server", "--runs-dir", str(tmp / "runs")], cwd=str(ROOT),
                          input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    assert proc.stderr == b"", proc.stderr[:300]
    out = proc.stdout.decode("utf-8")
    assert "GENERATION_FAILED" in out and "BAD_REQUEST" in out and "NO_EPISODE" in out and '"ok":true' in out


# ---------- public-данные генератора ----------
@pytest.mark.parametrize("name", list(CONFIGS))
@pytest.mark.parametrize("seed", [1, 2])
def test_public_has_no_hints(seed, name, tmp):
    """В public/ нет ID эталона, названий сценариев/типов/kind и слов-подсказок."""
    ep = generate_episode(seed, CONFIGS[name], tmp)
    found = la.public_hints(ep)
    msg = "; ".join("%s / %s: %r в %r" % x for x in found[:5])
    assert not found, "Подсказки в public/ (зона A): " + msg
