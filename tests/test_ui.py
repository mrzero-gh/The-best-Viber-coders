"""Тесты веб-интерфейса scripts.ui."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from scripts.ui import (
    export_standalone_html,
    find_episode_path,
    get_available_episodes,
    get_episode_details,
    run_policy_on_episode,
)

ROOT = Path(__file__).resolve().parent.parent


def test_get_available_episodes():
    episodes = get_available_episodes()
    assert len(episodes) >= 20
    ep_ids = [e["episode_id"] for e in episodes]
    assert "EP-0HJPAE81" in ep_ids
    assert "EP-4A84A8A4" in ep_ids
    sample = episodes[0]
    assert "mode" in sample
    assert "base_currency" in sample
    assert "counts" in sample
    assert "steps_count" in sample


def test_get_episode_details():
    details = get_episode_details("EP-0HJPAE81")
    assert details is not None
    assert details["episode_id"] == "EP-0HJPAE81"
    assert "manifest" in details
    assert len(details["invoices"]) > 0
    assert len(details["payments"]) > 0
    assert len(details["ledger"]) > 0
    assert len(details["fx"]) > 0
    assert details["truth"] is not None
    assert len(details["trajectory"]) > 0
    assert len(details["submitted_findings"]) > 0
    assert details["matcher_result"] is not None
    assert details["matcher_result"]["tp"] == 9
    assert details["matcher_result"]["fp"] == 0


def test_export_standalone_html():
    with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        export_standalone_html(tmp_path, "EP-0HJPAE81")
        assert tmp_path.exists()
        content = tmp_path.read_text(encoding="utf-8")
        assert "AUDIT GYM" in content
        assert "window.__INITIAL_DATA__" in content
        assert "EP-0HJPAE81" in content
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def test_run_policy_on_episode():
    ep_path = find_episode_path("EP-0HJPAE81")
    assert ep_path is not None
    res = run_policy_on_episode(ep_path, "oracle")
    assert res["policy_name"] == "oracle"
    assert len(res["trajectory"]) > 0
    assert res["eval_result"] is not None
    assert res["eval_result"]["valid"] is True
    assert res["eval_result"]["reward"] == 1.0


def test_ui_cli_export():
    with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        cmd = [sys.executable, "-m", "scripts.ui", "--export", str(tmp_path), "--episode", "EP-0HJPAE81"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        assert res.returncode == 0
        assert tmp_path.exists()
        assert "Автономный отчёт успешно экспортирован" in res.stdout
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
