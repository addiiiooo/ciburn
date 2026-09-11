from __future__ import annotations

from datetime import timedelta

import pytest

from ciburn.findings import Finding, Kind
from ciburn.join import _matches_tree, join_findings
from ciburn.pricing import Pricing
from ciburn.rules import HISTORY_RULES, STATIC_RULES, Context, run_rules
from ciburn.rules.history_rules import H001, H002, H003, H004, H005, H006, H007, H008, H009
from ciburn.workflow import Workflow, parse_workflow
from synth import T0, Synth

CI_YML = """
name: CI
on:
  push:
  pull_request:
  schedule:
    - cron: "*/30 * * * *"
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: ruff check .
  test:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        py: ["3.11", "3.12", "3.13", "3.14"]
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
      - name: Install dependencies
        run: pip install -e .
      - run: pytest
  e2e:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    services:
      db:
        image: postgres
    steps:
      - run: pytest tests/e2e
"""
PATH = ".github/workflows/ci.yml"


def wf() -> Workflow:
    return parse_workflow(CI_YML, PATH)


def ctx_for(pricing: Pricing, synth: Synth, workflows: list[Workflow]) -> Context:
    view = synth.view(pricing, workflows)
    from ciburn.repo_layout import RepoLayout

    return Context(
        workflows=workflows,
        pricing=pricing,
        model=pricing.model("2026"),
        layout=RepoLayout(top_level_dirs={"docs", "src"}, markdown_files=5),
        history=view,
        window_days=90,
    )


def findings_by_rule(fs: list[Finding]) -> dict[str, list[Finding]]:
    out: dict[str, list[Finding]] = {}
    for f in fs:
        out.setdefault(f.rule_id, []).append(f)
    return out


def build_rich_history(s: Synth) -> None:
    """A history that exercises most measurers and rules."""
    # PR runs on one branch, superseded: run A still running when run B starts
    a = s.run(
        PATH, "pull_request", "success", at=T0, head_sha="s1", branch="feat", wall_seconds=600
    )
    b = s.run(
        PATH, "pull_request", "success", at=T0 + timedelta(minutes=5), head_sha="s2", branch="feat"
    )
    for rid in (a, b):
        s.job(rid, "lint", 50, steps=[("Run actions/checkout@v4", 8), ("Run ruff check .", 40)])
        for py in ("3.11", "3.12", "3.13", "3.14"):
            s.job(
                rid,
                f"test ({py})",
                130,
                steps=[
                    ("Run actions/checkout@v4", 40),
                    ("Install dependencies", 60),
                    ("Run pytest", 30),
                ],
            )
        s.job(rid, "e2e", 900)
    # push run for the same sha as a PR run -> W009 duplicate
    p = s.run(PATH, "push", "success", at=T0 + timedelta(minutes=6), head_sha="s2", branch="feat")
    s.job(p, "lint", 50)
    s.job(p, "e2e", 900)
    # docs-only commit run -> W004
    d = s.run(PATH, "push", "success", at=T0 + timedelta(hours=1), head_sha="docs1", branch="main")
    s.job(d, "lint", 50)
    s.job(d, "e2e", 900)
    s.commit("docs1", ["docs/index.md", "README.md"])
    s.commit("s2", ["src/app.py"])
    # matrix run where one leg fails early and siblings keep running -> W012
    m = s.run(
        PATH, "pull_request", "failure", at=T0 + timedelta(hours=2), head_sha="s3", branch="feat2"
    )
    s.job(m, "test (3.11)", 30, conclusion="failure")
    s.job(m, "test (3.12)", 300)
    s.job(m, "test (3.13)", 300)
    s.job(m, "test (3.14)", 300)
    s.job(m, "lint", 50, conclusion="failure")
    s.job(m, "e2e", 900)  # ran in parallel with a failing cheap gate -> H009 needs >= 3 such runs
    for i in range(4):
        r = s.run(
            PATH,
            "pull_request",
            "failure",
            at=T0 + timedelta(hours=3 + i),
            head_sha=f"g{i}",
            branch="feat3",
        )
        s.job(r, "lint", 40, conclusion="failure")
        s.job(r, "e2e", 900)
        for py in ("3.11", "3.12", "3.13", "3.14"):
            s.job(r, f"test ({py})", 130)
    # re-run attempt -> H006
    rr = s.run(PATH, "push", "success", at=T0 + timedelta(hours=10), head_sha="rr", attempt=2)
    s.job(rr, "e2e", 900, attempt=1)
    s.job(rr, "e2e", 900, attempt=2)
    # scheduled runs: same sha repeated (H002), then a dead cron (H001)
    for i in range(6):
        r = s.run(PATH, "schedule", "success", at=T0 + timedelta(days=1, hours=i), head_sha="sched")
        s.job(r, "e2e", 600)
    for i in range(5):
        r = s.run(PATH, "schedule", "failure", at=T0 + timedelta(days=2, hours=i), head_sha="sched")
        s.job(r, "e2e", 600, conclusion="failure")
    # timeouts at the default -> H007
    t = s.run(PATH, "push", "failure", at=T0 + timedelta(days=3), head_sha="to")
    s.job(t, "test (3.12)", 360 * 60, conclusion="timed_out")  # `test` has no timeout-minutes
    # lots of successful lint jobs for H003/H005/H008 (queue heavy)
    for i in range(40):
        r = s.run(
            PATH, "push", "success", at=T0 + timedelta(days=4, minutes=i * 10), head_sha=f"ok{i}"
        )
        s.job(r, "lint", 60 if i % 10 else 600, queue_seconds=200)
        s.job(r, "e2e", 900 if i % 5 else 2700, conclusion="success" if i % 5 else "failure")


def test_join_measures_static_findings(pricing: Pricing) -> None:
    s = Synth()
    build_rich_history(s)
    workflows = [wf()]
    ctx = ctx_for(pricing, s, workflows)
    static = run_rules(ctx, STATIC_RULES)
    joined = join_findings(static, ctx)
    by = findings_by_rule(joined)
    measured = {rid for rid, fs in by.items() if any(f.kind is Kind.MEASURED for f in fs)}
    assert {"W001", "W002", "W003", "W004", "W006", "W009", "W010", "W012"} <= measured
    # W005 static needs >= 6 legs; the history-driven variant needs rounding >= 25 % of billed
    # minutes, which the 360-minute timed-out leg in this fixture dilutes. Covered separately.
    w001 = by["W001"][0]
    assert w001.recoverable_minutes_est
    assert w001.recoverable_minutes_est > 0
    assert w001.window_days == 90
    assert w001.observed_runs is not None
    assert "superseded" in (w001.estimate_method or "")
    w002 = next(f for f in by["W002"] if f.job == "test")
    assert w002.kind is Kind.MEASURED
    assert w002.recoverable_cost_est is not None
    assert "install" in (w002.estimate_method or "")
    w003 = next(f for f in by["W003"] if f.job == "test")
    assert w003.remediation.patch["value"] >= 5
    assert "timeout" in w003.remediation.summary
    w004 = by["W004"][0]
    assert w004.recoverable_minutes_est == 16.0  # docs-only run: lint 1 min + e2e 15 min
    w006 = by["W006"][0]
    assert w006.recoverable_minutes_est is not None
    assert w006.recoverable_minutes_est > 0
    w009 = by["W009"][0]
    assert w009.recoverable_minutes_est == 16.0
    w010 = next(f for f in by["W010"] if f.job == "e2e")
    assert w010.recoverable_cost_est is None
    assert "draft" in (w010.estimate_method or "")
    w012 = by["W012"][0]
    assert w012.recoverable_minutes_est is not None
    assert w012.recoverable_minutes_est > 0
    # W008: scheduled runs repeated on the same sha
    w008 = by["W008"][0]
    assert w008.kind is Kind.MEASURED
    assert w008.recoverable_minutes_est == 100.0  # 10 repeated scheduled runs x 10 min
    # every measured finding states a method and has evidence
    for f in joined:
        if f.kind is Kind.MEASURED:
            assert f.estimate_method
            assert f.evidence
            assert f.observed_minutes is not None


def test_join_leaves_findings_advisory_without_matching_history(pricing: Pricing) -> None:
    s = Synth()
    r = s.run(".github/workflows/other.yml", "push")
    s.job(r, "x", 60)
    workflows = [wf()]
    ctx = ctx_for(pricing, s, workflows)
    joined = join_findings(run_rules(ctx, STATIC_RULES), ctx)
    assert all(f.kind is Kind.ADVISORY for f in joined)
    assert all(f.observed_minutes is None for f in joined)
    ctx.history = None
    assert join_findings(run_rules(ctx, STATIC_RULES), ctx)


def test_history_rules_fire(pricing: Pricing) -> None:
    s = Synth()
    build_rich_history(s)
    workflows = [wf()]
    ctx = ctx_for(pricing, s, workflows)
    by = findings_by_rule(run_rules(ctx, HISTORY_RULES))
    assert set(by) >= {"H001", "H002", "H004", "H005", "H006", "H007", "H008", "H009"}
    h001 = by["H001"][0]
    assert h001.severity.value == "high"  # still failing at the tail
    assert h001.recoverable_minutes_est == 20.0  # 2 runs after the 3rd consecutive failure x 10 min
    assert "still failing" in h001.message
    h002 = by["H002"][0]
    assert h002.recoverable_minutes_est == 100.0
    h006 = by["H006"][0]
    assert h006.recoverable_minutes_est == 15.0
    h007 = by["H007"][0]
    assert h007.observed_minutes == 360.0
    assert h007.remediation.patch["key"] == "timeout-minutes"
    h009 = by["H009"][0]
    assert h009.job == "e2e"
    assert h009.remediation.patch["value"] == ["lint"]
    h005 = next(f for f in by["H005"] if f.job == "lint")
    assert h005.recoverable_minutes_est is not None
    assert h005.recoverable_minutes_est > 0
    h008 = next(f for f in by["H008"] if f.job == "lint")
    assert h008.recoverable_cost_est is None
    assert "latency-not-money" in h008.tags
    h004 = [f for f in by["H004"] if f.job == "e2e"]
    assert h004 == [] or h004[0].observed_minutes is not None
    for fs in by.values():
        for f in fs:
            assert f.kind is Kind.MEASURED
            assert f.estimate_method
            assert f.evidence


def test_h003_never_failed_and_gate_filter(pricing: Pricing) -> None:
    s = Synth()
    for i in range(35):
        r = s.run(PATH, "push", "success", at=T0 + timedelta(hours=i), head_sha=f"c{i}")
        s.job(r, "test (3.12)", 120)
        r2 = s.run(
            PATH, "schedule", "success", at=T0 + timedelta(hours=i, minutes=30), head_sha=f"c{i}"
        )
        s.job(r2, "nightly", 120)
    ctx = ctx_for(pricing, s, [wf()])
    by = findings_by_rule(run_rules(ctx, [H003()]))
    assert [f.job for f in by["H003"]] == ["test"]
    assert by["H003"][0].confidence.value == "low"
    assert "scenario" in (by["H003"][0].estimate_method or "")


def test_h001_medium_when_recovered(pricing: Pricing) -> None:
    s = Synth()
    for i in range(4):
        r = s.run(PATH, "schedule", "failure", at=T0 + timedelta(hours=i), head_sha="x")
        s.job(r, "e2e", 60, conclusion="failure")
    r = s.run(PATH, "schedule", "success", at=T0 + timedelta(hours=9), head_sha="y")
    s.job(r, "e2e", 60)
    ctx = ctx_for(pricing, s, [wf()])
    fs = run_rules(ctx, [H001()])
    assert len(fs) == 1
    assert fs[0].severity.value == "medium"
    assert fs[0].recoverable_minutes_est == 1.0


def test_h001_without_job_data(pricing: Pricing) -> None:
    s = Synth()
    for i in range(3):
        s.run(PATH, "schedule", "failure", at=T0 + timedelta(hours=i), head_sha="x")
    view = s.view(pricing, [wf()])
    for r in view.runs:
        r.jobs_fetched = False
    ctx = Context(workflows=[wf()], pricing=pricing, model=pricing.model("2026"), history=view)
    fs = run_rules(ctx, [H001()])
    assert fs[0].confidence.value == "medium"
    assert "no job data" in (fs[0].estimate_method or "")


def test_empty_history_yields_no_history_findings(pricing: Pricing) -> None:
    s = Synth()
    ctx = ctx_for(pricing, s, [wf()])
    assert run_rules(ctx, HISTORY_RULES) == []
    for rule in (H002(), H004(), H005(), H006(), H007(), H008(), H009()):
        assert rule.run(ctx) == []


def test_matches_tree() -> None:
    trees = ["docs/**", "**.md"]
    assert _matches_tree("docs/a/b.rst", trees)
    assert _matches_tree("README.md", trees)
    assert not _matches_tree("src/a.py", trees)
    assert _matches_tree("LICENSE", ["LICENSE"])


def test_w007_measure_and_slim_limit(pricing: Pricing) -> None:
    text = "on: push\njobs:\n  lint:\n    runs-on: ubuntu-latest\n    timeout-minutes: 5\n    steps:\n      - run: ruff check .\n"
    w = parse_workflow(text, PATH)
    s = Synth()
    for i in range(12):
        r = s.run(PATH, "push", at=T0 + timedelta(hours=i), head_sha=f"c{i}")
        s.job(r, "lint", 90)
    ctx = ctx_for(pricing, s, [w])
    fs = join_findings(run_rules(ctx, STATIC_RULES), ctx)
    w007 = next(f for f in fs if f.rule_id == "W007")
    assert w007.kind is Kind.MEASURED
    assert w007.observed_cost is not None
    assert w007.recoverable_cost_est == pytest.approx(
        w007.observed_cost * (1 - 0.002 / 0.006), rel=1e-6
    )
    assert w007.confidence.value == "medium"
    s2 = Synth()
    for i in range(3):
        r = s2.run(PATH, "push", at=T0 + timedelta(hours=i), head_sha=f"c{i}")
        s2.job(r, "lint", 14 * 60)
    ctx2 = ctx_for(pricing, s2, [w])
    w007b = next(
        f for f in join_findings(run_rules(ctx2, STATIC_RULES), ctx2) if f.rule_id == "W007"
    )
    assert w007b.recoverable_cost_est is None
    assert "too close" in (w007b.estimate_method or "")


def test_w002_without_install_steps_and_w006_without_baseline(pricing: Pricing) -> None:
    text = """
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
      - run: pip install -e .
"""
    w = parse_workflow(text, PATH)
    s = Synth()
    for i in range(3):
        r = s.run(PATH, "push", at=T0 + timedelta(hours=i), head_sha=f"c{i}")
        s.job(
            r,
            "test",
            100,
            steps=[("Run actions/checkout@v4", 30), ("Setup", 10), ("Run pip install -e .", 20)],
        )
    ctx = ctx_for(pricing, s, [w])
    fs = join_findings(run_rules(ctx, STATIC_RULES), ctx)
    w002 = next(f for f in fs if f.rule_id == "W002")
    assert w002.kind is Kind.MEASURED
    assert w002.recoverable_minutes_est is not None  # "Run pip install" matched via INSTALL_RE
    w006 = next(f for f in fs if f.rule_id == "W006")
    assert "assumed 10 s" in (w006.estimate_method or "")
    assert w006.confidence.value == "low"
    s3 = Synth()
    for i in range(3):
        r = s3.run(PATH, "push", at=T0 + timedelta(hours=i), head_sha=f"c{i}")
        s3.job(r, "test", 100, steps=[("Build", 100)])
    ctx3 = ctx_for(pricing, s3, [w])
    fs3 = join_findings(run_rules(ctx3, STATIC_RULES), ctx3)
    w002b = next(f for f in fs3 if f.rule_id == "W002")
    assert w002b.recoverable_minutes_est is None
    assert "could not be isolated" in (w002b.estimate_method or "")
    assert next(f for f in fs3 if f.rule_id == "W006").kind is Kind.ADVISORY


def test_sampled_history_is_labelled(pricing: Pricing) -> None:
    s = Synth()
    build_rich_history(s)
    view = s.view(pricing, [wf()])
    view.jobs_sampled = True
    ctx = Context(workflows=[wf()], pricing=pricing, model=pricing.model("2026"), history=view)
    fs = join_findings(run_rules(ctx, STATIC_RULES), ctx)
    assert any(
        "job data available for" in (f.estimate_method or "") for f in fs if f.kind is Kind.MEASURED
    )


def test_history_driven_w005_for_small_matrix(pricing: Pricing) -> None:
    s = Synth()
    for i in range(5):
        r = s.run(PATH, "push", at=T0 + timedelta(hours=i), head_sha=f"c{i}")
        for py in ("3.11", "3.12", "3.13", "3.14"):
            s.job(r, f"test ({py})", 130)  # 3 billed minutes for 2m10s of work
    ctx = ctx_for(pricing, s, [wf()])
    static = run_rules(ctx, STATIC_RULES)
    assert "W005" not in {f.rule_id for f in static}  # 4 legs: below the static threshold
    joined = join_findings(static, ctx)
    w005 = [f for f in joined if f.rule_id == "W005"]
    assert len(w005) == 1
    assert w005[0].kind is Kind.MEASURED
    assert w005[0].recoverable_minutes_est == 5 * (
        12 - 9
    )  # 4x3 billed vs ceil(520/60)=9 merged, per run
    assert "round-up" in w005[0].message
