from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ciburn.findings import Confidence, Finding, Kind, Remediation, Severity
from ciburn.fix import FixError, apply_patch, plan_fixes

BASE = """name: ci
on:
  push:
    branches: [main]
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        py: ["3.12"]
    steps:
      - uses: actions/checkout@v4
      - run: pytest
  lint:
    runs-on:
      - ubuntu-latest
    steps:
      - run: ruff check .
"""


def finding(
    rule: str,
    patch: dict[str, object],
    wf: str = ".github/workflows/ci.yml",
    job: str | None = None,
) -> Finding:
    return Finding(
        rule_id=rule,
        title=rule,
        severity=Severity.LOW,
        confidence=Confidence.HIGH,
        kind=Kind.ADVISORY,
        message="m",
        remediation=Remediation(summary="s", patch=patch),
        workflow=wf,
        job=job,
    )


def test_add_workflow_key() -> None:
    new, desc = apply_patch(
        BASE,
        {
            "op": "add_workflow_key",
            "key": "concurrency",
            "value": {"group": "${{ github.ref }}", "cancel-in-progress": True},
        },
    )
    data = yaml.safe_load(new)
    assert data["concurrency"] == {"group": "${{ github.ref }}", "cancel-in-progress": True}
    assert "added top-level" in desc
    assert new.index("concurrency:") < new.index("jobs:")
    with pytest.raises(FixError, match="already has"):
        apply_patch(new, {"op": "add_workflow_key", "key": "concurrency", "value": {}})


def test_add_and_set_job_key() -> None:
    new, _ = apply_patch(
        BASE, {"op": "add_job_key", "job": "test", "key": "timeout-minutes", "value": 15}
    )
    assert yaml.safe_load(new)["jobs"]["test"]["timeout-minutes"] == 15
    lines = new.splitlines()
    assert lines[lines.index("    runs-on: ubuntu-latest") + 1] == "    timeout-minutes: 15"
    with pytest.raises(FixError, match="already has"):
        apply_patch(
            new, {"op": "add_job_key", "job": "test", "key": "timeout-minutes", "value": 20}
        )
    new2, _ = apply_patch(
        new, {"op": "set_job_key", "job": "test", "key": "timeout-minutes", "value": 20}
    )
    assert yaml.safe_load(new2)["jobs"]["test"]["timeout-minutes"] == 20
    # multi-line runs-on list: key goes after the list
    new3, _ = apply_patch(
        BASE, {"op": "add_job_key", "job": "lint", "key": "timeout-minutes", "value": 5}
    )
    assert yaml.safe_load(new3)["jobs"]["lint"] == {
        "runs-on": ["ubuntu-latest"],
        "timeout-minutes": 5,
        "steps": [{"run": "ruff check ."}],
    }
    new4, _ = apply_patch(
        BASE, {"op": "set_job_key", "job": "lint", "key": "runs-on", "value": "ubuntu-slim"}
    )
    assert yaml.safe_load(new4)["jobs"]["lint"]["runs-on"] == "ubuntu-slim"
    new5, _ = apply_patch(
        BASE, {"op": "add_job_key", "job": "test", "key": "needs", "value": ["lint"]}
    )
    assert yaml.safe_load(new5)["jobs"]["test"]["needs"] == ["lint"]
    new6, _ = apply_patch(
        BASE,
        {
            "op": "add_job_key",
            "job": "test",
            "key": "if",
            "value": "github.event.pull_request.draft == false",
        },
    )
    assert yaml.safe_load(new6)["jobs"]["test"]["if"] == "github.event.pull_request.draft == false"
    with pytest.raises(FixError, match="could not find job"):
        apply_patch(BASE, {"op": "add_job_key", "job": "nope", "key": "x", "value": 1})


def test_set_strategy_key() -> None:
    new, _ = apply_patch(
        BASE, {"op": "set_strategy_key", "job": "test", "key": "fail-fast", "value": False}
    )
    assert yaml.safe_load(new)["jobs"]["test"]["strategy"]["fail-fast"] is False
    new2, _ = apply_patch(
        new, {"op": "set_strategy_key", "job": "test", "key": "fail-fast", "value": True}
    )
    assert yaml.safe_load(new2)["jobs"]["test"]["strategy"]["fail-fast"] is True
    with pytest.raises(FixError, match="no `strategy:`"):
        apply_patch(
            BASE, {"op": "set_strategy_key", "job": "lint", "key": "fail-fast", "value": False}
        )


def test_add_trigger_key() -> None:
    new, desc = apply_patch(
        BASE,
        {
            "op": "add_trigger_key",
            "events": ["push", "pull_request"],
            "key": "paths-ignore",
            "value": ["docs/**", "**.md"],
        },
    )
    data = yaml.safe_load(new)
    on = data.get("on", data.get(True))  # PyYAML reads a bare `on` key as True
    assert on["push"]["paths-ignore"] == ["docs/**", "**.md"]
    assert on["pull_request"]["paths-ignore"] == ["docs/**", "**.md"]
    assert on["push"]["branches"] == ["main"]
    assert "on.push, on.pull_request" in desc
    with pytest.raises(FixError, match="already has"):
        apply_patch(
            new, {"op": "add_trigger_key", "events": ["push"], "key": "paths-ignore", "value": []}
        )
    with pytest.raises(FixError, match="not a mapping"):
        apply_patch(
            "on: [push]\njobs: {}\n",
            {"op": "add_trigger_key", "events": ["push"], "key": "branches", "value": ["main"]},
        )
    with pytest.raises(FixError, match="could not find"):
        apply_patch(
            "on:\n  push:\njobs: {}\n",
            {"op": "add_trigger_key", "events": ["release"], "key": "branches", "value": ["main"]},
        )


def test_unsupported_and_invalid() -> None:
    with pytest.raises(FixError, match="unsupported"):
        apply_patch(BASE, {"op": "nope"})
    with pytest.raises(FixError, match="could not find top-level"):
        apply_patch(
            "name: x\non: push\n", {"op": "add_workflow_key", "key": "concurrency", "value": {}}
        )
    with pytest.raises(FixError, match="could not find top-level"):
        apply_patch(
            "name: x\non: push\n", {"op": "add_job_key", "job": "a", "key": "k", "value": 1}
        )


def test_plan_fixes(tmp_path: Path) -> None:
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "ci.yml").write_text(BASE, encoding="utf-8")
    fs = [
        finding(
            "W001",
            {
                "op": "add_workflow_key",
                "key": "concurrency",
                "value": {"group": "g", "cancel-in-progress": True},
            },
        ),
        finding(
            "W003",
            {"op": "add_job_key", "job": "test", "key": "timeout-minutes", "value": 10},
            job="test",
        ),
        finding(
            "W003",
            {"op": "add_job_key", "job": "lint", "key": "timeout-minutes", "value": 5},
            job="lint",
        ),
        finding("W002", {"op": "advise"}, job="test"),
        finding(
            "W003",
            {"op": "add_job_key", "job": "x", "key": "timeout-minutes", "value": 5},
            wf=".github/workflows/missing.yml",
            job="x",
        ),
        finding(
            "W003",
            {"op": "add_job_key", "job": "ghost", "key": "timeout-minutes", "value": 5},
            job="ghost",
        ),
    ]
    out = plan_fixes(fs, tmp_path)
    assert len(out.applied) == 3
    assert {r for _, r in out.skipped} >= {"no automatic fix for W002 (advise)"}
    assert any("not found" in r for _, r in out.skipped)
    assert any("could not find job" in r for _, r in out.skipped)
    assert out.patch.startswith("--- a/.github/workflows/ci.yml")
    new = yaml.safe_load(out.new_texts[".github/workflows/ci.yml"])
    assert new["concurrency"]["cancel-in-progress"] is True
    assert new["jobs"]["test"]["timeout-minutes"] == 10
    assert new["jobs"]["lint"]["timeout-minutes"] == 5
    only = plan_fixes(fs, tmp_path, {"W001"})
    assert len(only.applied) == 1
    assert plan_fixes([], tmp_path).patch == ""
