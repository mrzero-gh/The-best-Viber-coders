"""Настоящие данные проходят схемы контракта: эпизоды генератора, ответы среды, журнал оценщика.

Если формат генератора, среды или оценщика разойдётся со schemas/, этот тест упадёт.
"""
import copy
import json
import tempfile
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")
pytest.importorskip("referencing")
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

gen_config = pytest.importorskip("generator.config")
from env.client import AuditEnv
from env.episode import load_public
from generator.build import generate_episode

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "schemas"


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _registry():
    reg = Registry()
    for p in SCHEMAS.glob("*.schema.json"):
        s = _load(p)
        reg = reg.with_resource(s["$id"], Resource.from_contents(s, default_specification=DRAFT202012))
    return reg


REG = _registry()


def _validator(name, part=None):
    """Валидатор целой схемы или её части $defs/<part>."""
    schema = _load(SCHEMAS / (name + ".schema.json"))
    if part is not None:
        schema = {"$ref": schema["$id"] + "#/$defs/" + part}
    return jsonschema.Draft202012Validator(schema, registry=REG)


def _assert_valid(validator, obj, label):
    errors = sorted(validator.iter_errors(obj), key=lambda e: list(e.absolute_path))
    assert not errors, "%s: %s (путь %s)" % (label, errors[0].message[:200], list(errors[0].absolute_path))


def _configs():
    base = gen_config.DEFAULT_CONFIG
    with_interactions = copy.deepcopy(base)
    with_interactions["interactions"] = {s: {"enabled": True, "count": [1, 1]} for s in gen_config.INTERACTIONS}
    clean = copy.deepcopy(base)
    clean["clean"] = True
    return {"default": copy.deepcopy(base), "interactions": with_interactions, "clean": clean}


CONFIGS = _configs()
CASES = [(name, seed) for name in CONFIGS for seed in (1, 42)]


@pytest.mark.parametrize("name,seed", CASES, ids=lambda v: str(v))
def test_episode_files_match_schemas(name, seed):
    cfg = CONFIGS[name]
    _assert_valid(_validator("config"), cfg, "config")
    with tempfile.TemporaryDirectory() as tmp:
        ep = Path(generate_episode(seed, cfg, Path(tmp)))
        pub, priv = ep / "public", ep / "private"
        _assert_valid(_validator("manifest"), _load(pub / "manifest.json"), "manifest.json")
        _assert_valid(_validator("records", "policy"), _load(pub / "policy.json"), "policy.json")
        _assert_valid(_validator("truth"), _load(priv / "truth.json"), "truth.json")
        # Таблицы так, как их отдаёт среда (CSV уже приведён к типам и null)
        episode = load_public(ep)
        parts = {"ledger": "ledger_row", "invoices": "invoice", "payments": "payment", "fx": "fx_rate"}
        for table, part in parts.items():
            v = _validator("records", part)
            rows = episode.table(table)
            assert rows or table != "ledger", "пустая книга"
            for row in rows:
                _assert_valid(v, row, table)


def test_server_outputs_and_grader_log_match_schemas():
    """Ответы среды на сгенерированном эпизоде, строки траектории и журнал оценщика."""
    obs_v, traj_v, log_v = _validator("observation"), _validator("trajectory"), _validator("grader_log")
    cfg = copy.deepcopy(CONFIGS["interactions"])
    with tempfile.TemporaryDirectory() as tmp:
        for mode in ("train", "eval"):
            cfg["mode"] = mode
            with AuditEnv(runs_dir=Path(tmp) / "runs", run_id="schemas") as env:
                obs, info = env.reset(7, cfg)
                ep_id = info["episode_id"]
                steps = [(None, obs, 0.0, False, False)]
                actions = [
                    {"tool": "list_records", "args": {"table": "payments", "limit": 3}},
                    {"tool": "list_records", "args": {"table": "fx", "cursor": "c0", "limit": 1}},
                    {"tool": "get_record", "args": {"table": "ledger", "id": "L-00000000"}},
                    {"tool": "calculator", "args": {"expression": "(1 + 2) * 3 / 4"}},
                    {"tool": "submit", "args": {"findings": {"episode_id": ep_id, "findings": []}}},
                ]
                for action in actions:
                    o, r, term, trunc, _ = env.step(action)
                    steps.append((action, o, r, term, trunc))
                # документ из первой страницы платежей
                doc_id = steps[1][1]["result"]["records"][0]["doc_id"]
            for i, (action, o, r, term, trunc) in enumerate(steps):
                _assert_valid(obs_v, o, "observation step %d" % i)
                row = {"episode_id": ep_id, "step": i, "action": action, "observation": o,
                       "reward": r, "terminated": term, "truncated": trunc}
                _assert_valid(traj_v, row, "trajectory step %d" % i)
            assert doc_id.startswith("DOC-")
        log = [json.loads(line) for line in
               (Path(tmp) / "runs" / "schemas" / "grader_log.jsonl").read_text(encoding="utf-8").splitlines()]
        assert [x["mode"] for x in log] == ["train", "eval"]
        for row in log:
            _assert_valid(log_v, row, "grader_log")
