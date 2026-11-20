"""Ручные эталоны оценщика: не зависят от генератора A или среды B."""
from copy import deepcopy

EPISODE_ID = "EP-8K2M4Q7X"
TYPES = ("duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff")


def issue(kind="fx_conversion", number=1, amount=1000):
    return {
        "issue_id": f"ISS-{number:08d}", "type": kind,
        "affected_ids": [f"INV-{number:08d}"],
        "expected_amount_minor": amount, "currency": "KZT", "amount_tolerance_minor": 1,
        "evidence_sets": [[f"DOC-{number:08d}", f"L-{number:08d}"]], "scenario": None,
    }


def truth_for(*issues):
    return {"schema_version": "1.0", "episode_id": EPISODE_ID,
            "issues": deepcopy(list(issues)), "distractors": []}


def answer_for(truth):
    return {"episode_id": truth["episode_id"], "findings": [
        {"type": item["type"], "affected_ids": item["affected_ids"][:],
         "claimed_amount_minor": item["expected_amount_minor"], "currency": item["currency"],
         "evidence_ids": item["evidence_sets"][0][:], "explanation": "Ручное доказательство"}
        for item in truth["issues"]
    ]}
