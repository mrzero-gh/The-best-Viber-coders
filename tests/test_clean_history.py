"""Условие 1 блока D жюри: по provenance и открытым данным восстанавливается чистая история (21.11).

Чистая история = открытые таблицы - added + removed, с before вместо after. Её sha256 равен
provenance.clean_history_sha256, книга до внесения ошибок сбалансирована, сертификат это проверяет.
"""
import copy
import json
import subprocess
from pathlib import Path

import pytest

from generator.build import generate_episode
from grader import clean_history
from grader.certificate import certify
from grader.evidence import read_public

ROOT = Path(__file__).resolve().parent.parent
TYPES = ("duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff")
FULL = {"errors": {t: {"enabled": True, "count": [1, 2]} for t in TYPES},
        "interactions": {s: {"enabled": True, "count": [1, 1]}
                         for s in ("partial_fx_payment_at_cutoff", "duplicate_invoice_wrong_fx")}}


def _load(ep):
    ep = Path(ep)
    read = lambda name: json.loads((ep / "private" / name).read_text(encoding="utf-8"))  # noqa: E731
    return read_public(ep / "public"), read("truth.json"), read("provenance.json")


@pytest.mark.parametrize("seed", [1, 7, 42])
@pytest.mark.parametrize("cfg", [{}, FULL, {"clean": True}], ids=["default", "full", "clean"])
def test_generated_clean_history_is_reconstructed(tmp_path, seed, cfg):
    public, truth, prov = _load(generate_episode(seed, cfg, tmp_path))
    report = clean_history.check(public, prov)
    assert report["status"] == "ok", report
    assert report["sha256_matches"] and report["ledger_balance_minor"] == 0
    if cfg.get("clean"):
        assert all(not v for part in prov["clean_history_diff"].values() for v in part.values())
    else:
        assert prov["clean_history_diff"]["ledger"]["removed"]  # удалённые проводки сохранены целиком
        row = prov["clean_history_diff"]["ledger"]["removed"][0]
        assert set(row) == {"row_id", "timestamp", "account", "debit_minor", "credit_minor",
                            "currency", "entity_id", "invoice_id", "payment_id"}


def _tracked_episodes():
    """Сдаваемые эпизоды - те, что есть в общем trajectories.jsonl (git в Docker-образе нет)."""
    ids = {json.loads(line)["episode_id"]
           for line in (ROOT / "trajectories.jsonl").read_text(encoding="utf-8").splitlines() if line}
    return sorted(ep for split in ("train", "dev") for ep in (ROOT / "episodes" / split).glob("EP-*")
                  if ep.name in ids)


def test_committed_episodes_pass_clean_history_in_certificate():
    eps = _tracked_episodes()
    assert len(eps) == 20
    for ep in eps:
        public, truth, prov = _load(ep)
        report = certify(public, truth, prov)  # без записи certificate.json в эпизод
        assert report["clean_history_balance"]["status"] == "ok", ep.name
        assert report["valid"], ep.name


@pytest.fixture(scope="module")
def full_episode(tmp_path_factory):
    return _load(generate_episode(5, FULL, tmp_path_factory.mktemp("ch")))


def test_tampered_public_amount_fails(full_episode):
    public, truth, prov = copy.deepcopy(full_episode)
    added = set(prov["clean_history_diff"]["ledger"]["added"])
    row = next(r for r in public["ledger"] if r["row_id"] not in added)  # строка чистой истории
    row["debit_minor"] += 1
    report = clean_history.check(public, prov)
    assert report["status"] == "failed" and not report["sha256_matches"]
    assert certify(public, truth, prov)["valid"] is False


def test_incomplete_diff_fails(full_episode):
    public, truth, prov = copy.deepcopy(full_episode)
    prov["clean_history_diff"]["ledger"]["removed"].pop()
    report = clean_history.check(public, prov)
    assert report["status"] == "failed"
    assert certify(public, truth, prov)["valid"] is False


def test_diff_inconsistent_with_public_fails(full_episode):
    public, truth, prov = copy.deepcopy(full_episode)
    change = next(c for t in ("invoices", "payments") for c in prov["clean_history_diff"][t]["modified"])
    change["after"] = "2000-01-01T00:00:00+00:00"
    report = clean_history.check(public, prov)
    assert report["status"] == "failed" and report["errors"]


def test_unknown_added_id_fails(full_episode):
    public, truth, prov = copy.deepcopy(full_episode)
    prov["clean_history_diff"]["ledger"]["added"].append("L-ZZZZZZZZ")
    assert clean_history.check(public, prov)["status"] == "failed"


def test_without_diff_not_checked_and_not_a_complete_certificate(full_episode):
    public, truth, prov = copy.deepcopy(full_episode)
    del prov["clean_history_diff"]
    report = certify(public, truth, prov)
    assert report["clean_history_balance"]["status"] == "not_checked"
    assert report["valid"] is False
    assert certify(public, truth)["clean_history_balance"]["status"] == "not_checked"


def test_check_does_not_mutate_inputs(full_episode):
    public, truth, prov = copy.deepcopy(full_episode)
    before = copy.deepcopy((public, prov))
    clean_history.check(public, prov)
    assert (public, prov) == before


def test_cli(tmp_path):
    ep = generate_episode(3, {}, tmp_path)
    res = subprocess.run([__import__("sys").executable, "-m", "grader.clean_history", str(ep)],
                         cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    assert res.returncode == 0 and '"status":"ok"' in res.stdout
