"""Pytest wrapper для метаморфных и парных тестов."""
import pytest
from scripts.test_metamorphic import (
    test_determinism_fingerprint,
    test_paired_id_permutation,
    test_clean_and_distractors_invariants,
    test_policy_run_determinism,
    test_canary_leak_check,
)


def test_meta_determinism(tmp_path):
    test_determinism_fingerprint(tmp_path)


def test_meta_paired_ids(tmp_path):
    test_paired_id_permutation(tmp_path)


def test_meta_clean_invariants(tmp_path):
    test_clean_and_distractors_invariants(tmp_path)


def test_meta_run_determinism(tmp_path):
    test_policy_run_determinism(tmp_path)


def test_meta_canary(tmp_path):
    test_canary_leak_check(tmp_path)
