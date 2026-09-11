"""Unit tests for rule helper branches the golden fixtures do not reach."""

from __future__ import annotations

from ciburn.pricing import Pricing
from ciburn.rules import STATIC_RULES, Context, run_rules
from ciburn.rules.static_jobs import (
    _cache_suggestion,
    _is_light_step,
    _job_has_cache,
    _needs_full_history,
)
from ciburn.rules.static_matrix import _duplicate_legs
from ciburn.rules.static_triggers import _cancels_in_progress, _push_hits_feature_branches
from ciburn.workflow import Step, parse_workflow


def job(text: str):  # type: ignore[no-untyped-def]
    wf = parse_workflow(
        "on: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n" + text, "x.yml"
    )
    return wf.jobs["j"]


def test_job_has_cache_variants() -> None:
    assert _job_has_cache(
        job("      - uses: ruby/setup-ruby@v1\n        with:\n          bundler-cache: true\n")
    )
    assert not _job_has_cache(job("      - uses: ruby/setup-ruby@v1\n"))
    assert _job_has_cache(
        job(
            "      - uses: docker/build-push-action@v6\n        with:\n          cache-from: type=gha\n"
        )
    )
    assert not _job_has_cache(job("      - uses: docker/build-push-action@v6\n"))
    assert not _job_has_cache(
        job("      - uses: astral-sh/setup-uv@v5\n        with:\n          enable-cache: false\n")
    )
    assert _job_has_cache(job("      - uses: actions/setup-go@v5\n"))
    assert not _job_has_cache(
        job("      - uses: actions/setup-go@v5\n        with:\n          cache: false\n")
    )
    assert _job_has_cache(
        job("      - uses: actions/setup-node@v4\n        with:\n          cache: npm\n")
    )
    assert _job_has_cache(job("      - uses: gradle/actions/setup-gradle@v4\n"))
    assert _job_has_cache(job("      - run: echo use actions/cache to restore\n"))
    assert not _job_has_cache(job("      - run: npm ci\n"))


def test_cache_suggestions() -> None:
    def s(run: str | None = None, uses: str | None = None) -> Step:
        return Step(index=0, name=None, uses=uses, run=run, with_={}, if_=None)

    assert "setup-uv" in _cache_suggestion([s("uv sync")])
    assert "setup-python" in _cache_suggestion([s("pip install -r r.txt")])
    assert "pypoetry" in _cache_suggestion([s("poetry install")])
    assert "setup-node" in _cache_suggestion([s("pnpm install")])
    assert "rust-cache" in _cache_suggestion([s("cargo build")])
    assert "setup-go" in _cache_suggestion([s("go build ./...")])
    assert "bundler-cache" in _cache_suggestion([s("bundle install")])
    assert "setup-java" in _cache_suggestion([s("mvn -B package")])
    assert "apt" in _cache_suggestion([s("sudo apt-get install -y foo")])
    assert "actions/cache" in _cache_suggestion([s("composer install")])
    assert "setup-node" in _cache_suggestion([s(uses="actions/setup-node@v4")])


def test_light_steps() -> None:
    def s(run: str | None = None, uses: str | None = None) -> Step:
        return Step(index=0, name=None, uses=uses, run=run, with_={}, if_=None)

    assert _is_light_step(s(uses="actions/checkout@v4"))
    assert not _is_light_step(s(uses="./my-action"))
    assert not _is_light_step(s(uses="docker/build-push-action@v6"))
    assert _is_light_step(s(run="# comment only\n"))
    assert _is_light_step(s(run="ruff check .\nmypy src\n"))
    assert not _is_light_step(s(run="ruff check .\npytest\n"))
    assert _is_light_step(s())


def test_needs_full_history() -> None:
    assert _needs_full_history(
        job(
            "      - uses: actions/checkout@v4\n        with:\n          fetch-depth: 0\n          fetch-tags: true\n"
        )
    )
    assert _needs_full_history(job("      - run: python -m setuptools_scm\n"))
    assert _needs_full_history(job("      - uses: codecov/codecov-action@v4\n"))
    assert not _needs_full_history(job("      - run: pytest\n"))
    wf = parse_workflow(
        "on: push\njobs:\n  release:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo\n",
        "x.yml",
    )
    assert _needs_full_history(wf.jobs["release"])


def test_duplicate_legs() -> None:
    assert _duplicate_legs(
        {"os": ["a", "a"], "include": [{"x": 1}, {"x": 1}], "exclude": "${{ e }}"}
    ) == [
        "os=a",
        'include={"x": 1}',
    ]
    assert _duplicate_legs({"os": "${{ fromJson(x) }}"}) == []


def test_cancels_in_progress_forms() -> None:
    assert not _cancels_in_progress(None)
    assert not _cancels_in_progress("group-only")
    assert not _cancels_in_progress({"group": "g"})
    assert not _cancels_in_progress({"group": "g", "cancel-in-progress": False})
    assert _cancels_in_progress({"group": "g", "cancel-in-progress": True})
    assert _cancels_in_progress({"group": "g", "cancel-in-progress": "true"})
    assert _cancels_in_progress({"group": "g", "cancel-in-progress": "${{ x }}"})
    assert not _cancels_in_progress({"group": "g", "cancel-in-progress": "no"})


def test_push_feature_branch_detection() -> None:
    assert _push_hits_feature_branches({})
    assert _push_hits_feature_branches({"branches-ignore": ["main"]})
    assert not _push_hits_feature_branches({"tags": ["v*"]})
    assert not _push_hits_feature_branches({"branches": ["main", "release/1.x"]})
    assert not _push_hits_feature_branches({"branches": "main"})
    assert _push_hits_feature_branches({"branches": ["**"]})
    assert _push_hits_feature_branches({"branches": ["feature/*"]})
    assert _push_hits_feature_branches({"branches": {"weird": 1}})


def test_job_level_concurrency_and_workflow_timeout(pricing: Pricing) -> None:
    text = """
on: pull_request
timeout-minutes: 30
jobs:
  a:
    runs-on: ubuntu-latest
    concurrency:
      group: a-${{ github.ref }}
      cancel-in-progress: true
    steps:
      - run: pytest
  b:
    runs-on: ubuntu-latest
    concurrency:
      group: b-${{ github.ref }}
      cancel-in-progress: true
    steps:
      - run: pytest
"""
    wf = parse_workflow(text, "x.yml")
    ctx = Context(workflows=[wf], pricing=pricing, model=pricing.model("2026"))
    ids = {f.rule_id for f in run_rules(ctx, STATIC_RULES)}
    assert "W001" not in ids
    assert "W003" not in ids


def test_w010_ready_for_review_and_workflow_if(pricing: Pricing) -> None:
    text = """
on:
  pull_request:
    types: [ready_for_review]
jobs:
  e2e:
    runs-on: macos-latest
    steps:
      - run: pytest
"""
    wf = parse_workflow(text, "x.yml")
    ctx = Context(workflows=[wf], pricing=pricing, model=pricing.model("2026"))
    assert "W010" not in {f.rule_id for f in run_rules(ctx, STATIC_RULES)}
    text2 = """
on: pull_request
jobs:
  e2e:
    runs-on: macos-latest
    steps:
      - run: pytest
  win:
    runs-on: windows-latest
    timeout-minutes: 60
    steps:
      - run: pytest
"""
    wf2 = parse_workflow(text2, "x.yml")
    ctx2 = Context(workflows=[wf2], pricing=pricing, model=pricing.model("2026"))
    w010 = [f for f in run_rules(ctx2, STATIC_RULES) if f.rule_id == "W010"]
    assert {f.job for f in w010} == {"e2e", "win"}
    assert "macOS/Windows runner" in w010[0].message


def test_w008_skips_unparsable_cron_and_w005_unknown_legs(pricing: Pricing) -> None:
    text = """
on:
  schedule:
    - cron: "not a cron"
    - cron: "* * * * *"
jobs:
  m:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    strategy:
      matrix:
        os: ${{ fromJson(vars.OS) }}
    steps:
      - run: pytest
"""
    wf = parse_workflow(text, "x.yml")
    ctx = Context(workflows=[wf], pricing=pricing, model=pricing.model("2026"))
    fs = run_rules(ctx, STATIC_RULES)
    w008 = [f for f in fs if f.rule_id == "W008"]
    assert len(w008) == 1
    assert w008[0].severity.value == "medium"
    assert "W005" not in {f.rule_id for f in fs}


def test_w007_skips_expression_runner_and_w011_multiline(pricing: Pricing) -> None:
    text = """
on: push
jobs:
  a:
    runs-on: ${{ vars.RUNNER }}
    timeout-minutes: 5
    steps:
      - run: ruff check .
  b:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/upload-artifact@v4
        with:
          path: |
            dist/*.whl
            build/
"""
    wf = parse_workflow(text, "x.yml")
    ctx = Context(workflows=[wf], pricing=pricing, model=pricing.model("2026"))
    fs = run_rules(ctx, STATIC_RULES)
    assert [f.job for f in fs if f.rule_id == "W007"] == ["b"]  # `a` skipped: expression runner
    assert [f.job for f in fs if f.rule_id == "W011"] == ["b"]


def test_ignore_comment_and_flag(pricing: Pricing) -> None:
    text = """
# ciburn-ignore: W003, w001
on: pull_request
jobs:
  a:
    runs-on: ubuntu-latest
    steps:
      - run: pytest
"""
    wf = parse_workflow(text, "x.yml")
    assert wf.ignored_rules == {"W003", "W001"}
    ctx = Context(workflows=[wf], pricing=pricing, model=pricing.model("2026"))
    ids = {f.rule_id for f in run_rules(ctx, STATIC_RULES)}
    assert "W003" not in ids
    assert "W001" not in ids
    ctx.ignore = {"W010"}
    assert "W010" not in {f.rule_id for f in run_rules(ctx, STATIC_RULES)}
