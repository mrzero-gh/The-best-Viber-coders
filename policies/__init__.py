"""Политики агентов для Audit Gym."""
from policies.base import Policy
from policies.flag_all import FlagAllPolicy
from policies.oracle import OraclePolicy
from policies.random_policy import RandomPolicy
from policies.rule_based import RuleBasedPolicy, SimpleRulePolicy

POLICIES = {
    "oracle": OraclePolicy,
    "rule_based": RuleBasedPolicy,
    "simple_rule": SimpleRulePolicy,
    "flag_all": FlagAllPolicy,
    "random": RandomPolicy,
}

__all__ = [
    "Policy",
    "OraclePolicy",
    "RuleBasedPolicy",
    "SimpleRulePolicy",
    "FlagAllPolicy",
    "RandomPolicy",
    "POLICIES",
]
