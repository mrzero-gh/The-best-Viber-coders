"""Тесты серверного процесса среды через настоящий subprocess (env.client.AuditEnv)."""
import json
import tempfile
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")
pytest.importorskip("referencing")
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from common.jsonio import canonical_dumps
from env.client import AuditEnv, EnvError

ROOT = Path(__file__).resolve().parent.parent
EP_DIR = ROOT / "tests" / "fixtures" / "mini" / "EP-8K2M4Q7X"
GRADER = "tests.fake_grader:evaluate"
FINDINGS = {"episode_id": "EP-8K2M4Q7X", "findings": []}
LIST = {"tool": "list_records", "args": {"table": "ledger"}}
SUBMIT = {"tool": "submit", "args": {"findings": FINDINGS}}


def _schema(name):
    return json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))


def _validators():
    reg = Registry()
    for p in (ROOT / "schemas").glob("*.schema.json"):
        s = json.loads(p.read_text(encoding="utf-8"))
        reg = reg.with_resource(s["$id"], Resource.from_contents(s, default_specification=DRAFT202012))
    mk = lambda n: jsonschema.Draft202012Validator(_schema(n), registry=reg)
    return mk("observation.schema.json"), mk("trajectory.schema.json")


OBS_V, TRAJ_V = _validators()


class Run:
    """Запуск среды с эпизодом-фикстурой, фейковым оценщиком и журналом во временной папке."""

    def __init__(self, tmp, **kw):
        self.runs_dir = Path(tmp) / "runs"
        opts = dict(episode=EP_DIR, grader=GRADER, run_id="r1", runs_dir=self.runs_dir)
        opts.update(kw)
        self.env = AuditEnv(**opts)
        self.responses = []  # все ответы за эпизод (для проверки утечек)

    def reset(self, mode="train", seed=1):
        obs, info = self.env.reset(seed, {"mode": mode})
        OBS_V.validate(obs)
        self.responses.append((obs, info))
        self._traj(obs, 0, None, 0.0, False, False)
        return obs, info

    def step(self, action):
        out = self.env.step(action)
        obs, reward, term, trunc, info = out
        OBS_V.validate(obs)
        self._traj(obs, obs.get("steps_used", 1), action, reward, term, trunc)
        self.responses.append(out)
        return out

    def _traj(self, obs, step, action, reward, term, trunc):
        TRAJ_V.validate({"episode_id": "EP-8K2M4Q7X", "step": step, "action": action,
                         "observation": obs, "reward": reward, "terminated": term, "truncated": trunc})

    def log(self):
        p = self.runs_dir / "r1" / "grader_log.jsonl"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def tmp():
    with tempfile.TemporaryDirectory() as d:
        yield d


def _full_episode(r, mode):
    r.reset(mode)
    r.step(LIST)
    return r.step(SUBMIT)


def test_train_flow(tmp):
    r = Run(tmp)
    with r.env:
        obs, reward, term, trunc, info = _full_episode(r, "train")
    assert reward == 0.5 and term and not trunc
    assert obs["status"] == "submitted"
    assert "metrics" in info
    rows = r.log()
    assert len(rows) == 1
    row = rows[0]
    assert row["run_id"] == "r1" and row["episode_id"] == "EP-8K2M4Q7X" and row["mode"] == "train"
    assert row["seed"] is None and row["config_version"] is None
    assert row["status"] == "submitted" and row["steps_used"] == 2 and row["inspection_steps"] == 1
    assert row["reward"] == 0.5 and row["valid"] is True and row["error"] is None
    assert row["metrics"]["tp"] == 1


def test_eval_hides_reward(tmp):
    r = Run(tmp)
    with r.env:
        obs, reward, term, trunc, info = _full_episode(r, "eval")
    assert reward == 0.0 and term
    assert info == {"episode_id": "EP-8K2M4Q7X", "mode": "eval"}
    assert "metrics" not in info
    assert r.log()[0]["reward"] == 0.5


@pytest.mark.parametrize("mode", ["train", "eval"])
def test_no_leaks(tmp, mode):
    r = Run(tmp)
    with r.env:
        _full_episode(r, mode)
    truth = json.loads((EP_DIR / "private" / "truth.json").read_text(encoding="utf-8"))
    blob = canonical_dumps(r.responses)
    forbidden = [i["issue_id"] for i in truth["issues"]]
    forbidden += ["truth", "provenance", "private", "grader_log", str(EP_DIR), str(r.runs_dir),
                  str(EP_DIR).replace("\\", "/")]
    for s in forbidden:
        assert s not in blob, s


def test_step_before_reset(tmp):
    r = Run(tmp)
    with r.env:
        with pytest.raises(EnvError) as e:
            r.env.step(LIST)
        assert e.value.code == "NO_EPISODE"
        r.reset()


def _raw(env, data: bytes) -> dict:
    """Пишет сырые байты прямо в пайп сервера и читает один ответ."""
    env._proc.stdin.write(data)
    env._proc.stdin.flush()
    return json.loads(env._proc.stdout.readline().decode("utf-8"))


def test_garbage_and_long_line(tmp):
    r = Run(tmp)
    with r.env:
        for data in (b"not json\n", b"[1]\n", b'{"cmd":"zzz"}\n',
                     b" " * (1024 * 1024 + 10) + b"\n"):
            assert _raw(r.env, data)["error"]["code"] == "BAD_REQUEST"
        r.reset()  # сервер жив


def test_bad_config_and_seed(tmp):
    r = Run(tmp)
    with r.env:
        for cfg in ([], "x", {"mode": "x"}, {}):
            with pytest.raises(EnvError) as e:
                r.env.reset(1, cfg)
            assert e.value.code == "BAD_CONFIG"
        with pytest.raises(EnvError) as e:
            r.env.reset(True, {"mode": "train"})
        assert e.value.code == "BAD_REQUEST"
        r.reset()


def test_two_resets_independent(tmp):
    r = Run(tmp)
    with r.env:
        r.reset()
        r.step(LIST)
        obs, *_ = r.step(LIST)
        assert obs["steps_used"] == 2
        init, _ = r.reset()
        assert init["steps_used"] == 0
        obs, *_ = r.step(LIST)
        assert obs["steps_used"] == 1
        r.step(SUBMIT)
        init, _ = r.reset()
        assert init["steps_used"] == 0
        obs, *_ = r.step(LIST)
        assert obs["kind"] == "result" and obs["steps_used"] == 1


def test_step_after_finish(tmp):
    r = Run(tmp)
    with r.env:
        _full_episode(r, "train")
        obs, reward, term, trunc, info = r.env.step(LIST)
    assert obs["kind"] == "error" and obs["error"]["code"] == "EPISODE_FINISHED"
    assert reward == 0.0 and term
    assert len(r.log()) == 1  # повторного вызова оценщика и записи в журнал нет


def test_time_limit(tmp):
    # отрицательный лимит: на Windows у monotonic грубое разрешение, 0.0 может не сработать
    r = Run(tmp, time_limit_s=-1.0)
    with r.env:
        r.reset()
        obs, reward, term, trunc, info = r.step(LIST)
    assert obs["status"] == "time_limit" and trunc and not term
    assert r.log()[0]["status"] == "time_limit"


def test_generation_failed(tmp):
    r = Run(tmp, episode=None)
    with r.env:
        with pytest.raises(EnvError) as e:
            r.env.reset(1, {"mode": "train"})
        assert e.value.code == "GENERATION_FAILED"
        with pytest.raises(EnvError) as e:  # сервер жив
            r.env.reset(2, {"mode": "eval"})
        assert e.value.code == "GENERATION_FAILED"


def test_close(tmp):
    r = Run(tmp)
    with r.env:
        r.reset()
    assert r.env._proc.returncode == 0
    r.env.close()  # повторный close не падает


def test_raw_fd1_output_does_not_break_protocol():
    """Вывод прямо в fd 1 из оценщика уходит в stderr, а не в протокол."""
    with tempfile.TemporaryDirectory() as tmp:
        r = Run(tmp, grader="tests.fake_grader:evaluate_fd_noise")
        try:
            r.reset(mode="eval")
            obs, reward, term, trunc, info = r.step(SUBMIT)
            assert obs["status"] == "submitted" and reward == 0.0 and term
        finally:
            r.env.close()
