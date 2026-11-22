"""Веб-интерфейс: «Запустить аудит» и суммы в карточках находок (scripts.ui).

Раньше после запуска политики интерфейс получал сопоставление без matches и помечал все верные
находки как FP, а суммы в minor units показывал как целые числа (47 983,08 RUB как «4 798 308»).
"""
from scripts import ui


def test_run_returns_matcher_result_with_matches():
    ep = ui.find_episode_path("EP-0HJPAE81")
    res = ui.run_policy_on_episode(ep, "smart")
    m = res["matcher_result"]
    assert len(res["submitted_findings"]) == 9
    assert m["tp"] == 9 and m["fp"] == 0 and m["fn"] == 0
    assert sorted(x["finding_index"] for x in m["matches"]) == list(range(9))


def test_amounts_use_currency_exponent():
    html = ui.HTML_TEMPLATE
    assert "toLocaleString" not in html
    assert "money(f.claimed_amount_minor, f.currency)" in html
    assert "money(iss.expected_amount_minor, iss.currency)" in html
    assert "window.activeEpisode.matcher_result = res.matcher_result || null;" in html
