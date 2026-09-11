"""Rule registry."""

from __future__ import annotations

from ciburn.rules.base import Context, Rule, run_rules
from ciburn.rules.static_jobs import W002, W003, W006, W007, W011
from ciburn.rules.static_matrix import W005, W012
from ciburn.rules.static_triggers import W000, W001, W004, W008, W009, W010

STATIC_RULES: list[Rule] = [
    W000(),
    W001(),
    W002(),
    W003(),
    W004(),
    W005(),
    W006(),
    W007(),
    W008(),
    W009(),
    W010(),
    W011(),
    W012(),
]

__all__ = ["STATIC_RULES", "Context", "Rule", "run_rules"]
