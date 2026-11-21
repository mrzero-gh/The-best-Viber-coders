"""Устойчивость среды и адаптера жюри к окружению и повреждённым эпизодам (находки ревью 20.11).

1. журнал оценщика не записывается -> агент всё равно получает итог эпизода;
2. относительный episode_dir считается от папки вызывающего, а не от корня репозитория;
3. повторная генерация в ту же папку заменяет эпизод, а не вкладывает новый внутрь;
4. оценщик отклонил эталон -> EPISODE_INVALID, эпизод закрыт;
5. public-файл не разбирается -> EPISODE_INVALID, а не INTERNAL;
6. score_episode принимает целое число шагов в виде float.
"""
import json

import pytest

import judge_adapter as ja
from env.client import AuditEnv, EnvError
from env.core import AuditCore, GraderError
from env.episode import load_public, load_truth


def _submit_empty(env, episode_id):
    return env.step({"tool": "submit", "args": {"findings": {"episode_id": episode_id, "findings": []}}})


def test_submit_survives_unwritable_grader_log(tmp_path):
    blocker = tmp_path / "runs_is_a_file"
    blocker.write_text("x", encoding="utf-8")
    with AuditEnv(run_id="r", runs_dir=blocker) as env:
        _, info = env.reset(1, {"mode": "train"})
        obs, reward, term, trunc, _ = _submit_empty(env, info["episode_id"])
    assert obs["kind"] == "final" and obs["status"] == "submitted"
    assert term and not trunc and isinstance(reward, float)


def test_relative_episode_dir_is_resolved_from_caller_cwd(tmp_path, monkeypatch):
    ja.generate_episode(5, {}, str(tmp_path / "ep"))
    monkeypatch.chdir(tmp_path)
    with ja.make_env() as env:
        first, info = env.reset(0, {"mode": "closed", "episode_dir": "ep", "team_config": {}})
    assert info["mode"] == "closed"
    assert first["manifest"]["episode_id"] == info["episode_id"]


def test_default_runs_dir_is_absolute(tmp_path, monkeypatch):
    monkeypatch.delenv("AUDIT_GYM_RUNS_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert ja._runs_dir() == (tmp_path / "runs").resolve()


def test_generate_twice_into_same_dir_replaces_episode(tmp_path):
    out = tmp_path / "ep"
    ja.generate_episode(1, {}, str(out))
    first_id = load_public(out).episode_id
    ja.generate_episode(2, {}, str(out))
    assert not (out / "public" / "public").exists()
    assert not (out / "private" / "private").exists()
    second = load_public(out)
    assert second.episode_id != first_id
    assert load_truth(out)["episode_id"] == second.episode_id
    assert not list(out.glob(".gen-*"))


def test_core_closes_episode_when_grader_rejects_truth(tmp_path):
    ja.generate_episode(3, {}, str(tmp_path / "ep"))
    ep = tmp_path / "ep"

    def bad_grader(*args, **kwargs):
        raise ValueError("truth: duplicate issue ID")

    core = AuditCore(load_public(ep), load_truth(ep), mode="train", grader=bad_grader)
    with pytest.raises(GraderError):
        core.step({"tool": "submit", "args": {"findings": {"episode_id": core.episode_id, "findings": []}}})
    assert core.finished
    obs, *_ = core.step({"tool": "list_records", "args": {"table": "ledger"}})
    assert obs["kind"] == "error" and obs["error"]["code"] == "EPISODE_FINISHED"


def test_server_reports_episode_invalid_for_broken_truth(tmp_path):
    ep = tmp_path / "ep"
    ja.generate_episode(3, {}, str(ep))
    truth_path = ep / "private" / "truth.json"
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    truth["episode_id"] = "EP-00000000"  # эталон чужого эпизода
    truth_path.write_text(json.dumps(truth), encoding="utf-8")
    with ja.make_env() as env:
        _, info = env.reset(0, {"mode": "closed", "episode_dir": str(ep)})
        with pytest.raises(EnvError) as e:
            env.step({"name": "submit", "args": {"findings": {"episode_id": info["episode_id"],
                                                              "findings": []}}})
        assert e.value.code == "EPISODE_INVALID"
        out, *_ = env.step({"name": "list_records", "args": {"table": "ledger"}})
        assert out["error"]["code"] == "EPISODE_FINISHED"


@pytest.mark.parametrize("name, content", [
    ("manifest.json", '{"episode_id": "EP-'),
    ("ledger.csv", "row_id,timestamp\nL-00000000,2026-01-01T00:00:00+05:00\n"),
])
def test_unparsable_public_file_is_episode_invalid(tmp_path, name, content):
    ep = tmp_path / "ep"
    ja.generate_episode(4, {}, str(ep))
    (ep / "public" / name).write_text(content, encoding="utf-8")
    with ja.make_env() as env:
        with pytest.raises(EnvError) as e:
            env.reset(0, {"mode": "closed", "episode_dir": str(ep)})
    assert e.value.code == "EPISODE_INVALID"


def test_score_episode_accepts_integral_float_steps(tmp_path):
    ep = tmp_path / "ep"
    ja.generate_episode(6, {}, str(ep))
    fin = {"episode_id": load_public(ep).episode_id, "findings": []}
    assert ja.score_episode(str(ep), fin, 3.0) == ja.score_episode(str(ep), fin, 3)
    with pytest.raises(ValueError):
        ja.score_episode(str(ep), fin, 2.5)
