"""Golden-file tests: every fixture workflow in tests/fixtures/workflows is analysed with
all static rules; the set of (rule, job) pairs must equal tests/fixtures/expected/<name>.json.

Naming convention: ``<RULE>_positive*.yml`` must trigger ``<RULE>``;
``<RULE>_negative*.yml`` must not. ``clean.yml`` must trigger nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ciburn.findings import Finding, Kind
from ciburn.pricing import Pricing
from ciburn.repo_layout import RepoLayout
from ciburn.rules import STATIC_RULES, Context, run_rules
from ciburn.workflow import parse_workflow

WF_DIR = Path(__file__).parent / "fixtures" / "workflows"
EXPECTED_DIR = Path(__file__).parent / "fixtures" / "expected"
FIXTURES = sorted(p.stem for p in WF_DIR.glob("*.yml"))

# A layout with separable trees so W004 can fire on unfiltered triggers.
LAYOUT = RepoLayout(top_level_dirs={"src", "docs", "tests"}, markdown_files=5, total_files=100)


def analyse(name: str, pricing: Pricing) -> list[Finding]:
    text = (WF_DIR / f"{name}.yml").read_text(encoding="utf-8")
    wf = parse_workflow(text, f".github/workflows/{name}.yml")
    ctx = Context(workflows=[wf], pricing=pricing, model=pricing.model("2026"), layout=LAYOUT)
    return run_rules(ctx, STATIC_RULES)


def summary(findings: list[Finding]) -> list[dict[str, str | None]]:
    return sorted(
        ({"rule": f.rule_id, "job": f.job, "severity": f.severity.value} for f in findings),
        key=lambda d: (d["rule"] or "", d["job"] or ""),
    )


@pytest.mark.parametrize("name", FIXTURES)
def test_golden(name: str, pricing: Pricing) -> None:
    findings = analyse(name, pricing)
    expected_path = EXPECTED_DIR / f"{name}.json"
    assert expected_path.exists(), (
        f"missing golden file {expected_path}; run scripts/regen_golden.py"
    )
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    assert summary(findings) == expected


@pytest.mark.parametrize("name", [n for n in FIXTURES if "_positive" in n])
def test_positive_fixture_triggers_its_rule(name: str, pricing: Pricing) -> None:
    rule = name.split("_", maxsplit=1)[0]
    assert rule in {f.rule_id for f in analyse(name, pricing)}


@pytest.mark.parametrize("name", [n for n in FIXTURES if "_negative" in n])
def test_negative_fixture_does_not_trigger_its_rule(name: str, pricing: Pricing) -> None:
    rule = name.split("_", maxsplit=1)[0]
    assert rule not in {f.rule_id for f in analyse(name, pricing)}


def test_clean_fixture_is_clean(pricing: Pricing) -> None:
    assert analyse("clean", pricing) == []


def test_static_findings_are_advisory_and_carry_no_numbers(pricing: Pricing) -> None:
    for name in FIXTURES:
        for f in analyse(name, pricing):
            assert f.kind is Kind.ADVISORY
            assert f.observed_minutes is None
            assert f.recoverable_cost_est is None
            assert f.evidence, f.rule_id
            assert f.remediation.summary


def test_every_rule_has_positive_and_negative_fixture() -> None:
    for rule in STATIC_RULES:
        positives = [n for n in FIXTURES if n.startswith(rule.id + "_positive")]
        negatives = [n for n in FIXTURES if n.startswith(rule.id + "_negative")]
        assert positives, f"{rule.id} has no positive fixture"
        assert negatives or rule.id == "W000", f"{rule.id} has no negative fixture"


def test_w004_needs_layout(pricing: Pricing) -> None:
    text = (WF_DIR / "W004_positive.yml").read_text(encoding="utf-8")
    wf = parse_workflow(text, ".github/workflows/x.yml")
    ctx = Context(workflows=[wf], pricing=pricing, model=pricing.model("2026"), layout=None)
    assert "W004" not in {f.rule_id for f in run_rules(ctx, STATIC_RULES)}
    ctx.layout = RepoLayout(top_level_dirs={"src"}, markdown_files=1)
    assert "W004" not in {f.rule_id for f in run_rules(ctx, STATIC_RULES)}


def test_history_rules_are_skipped_without_history(pricing: Pricing) -> None:
    class Fake:
        id = "H999"
        title = "fake"
        severity = "low"
        needs_history = True

        def run(self, ctx: Context) -> list[Finding]:  # pragma: no cover
            raise AssertionError("must not run")

    ctx = Context(workflows=[], pricing=pricing, model=pricing.model("2026"))
    assert run_rules(ctx, [Fake()]) == []  # type: ignore[list-item]
