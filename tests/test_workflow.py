from __future__ import annotations

from pathlib import Path

from ciburn.workflow import (
    Job,
    Step,
    load_workflow_texts,
    load_workflows,
    matrix_leg_count,
    parse_workflow,
    workflow_files,
)


def test_on_forms() -> None:
    assert parse_workflow("on: push\njobs: {}\n", "a.yml").events == ["push"]
    assert parse_workflow("on: [push, pull_request]\njobs: {}\n", "a.yml").events == [
        "push",
        "pull_request",
    ]
    wf = parse_workflow(
        "on:\n  push:\n    branches: [main]\n  schedule:\n    - cron: '0 0 * * *'\njobs: {}\n",
        "a.yml",
    )
    assert wf.on["push"]["branches"] == ["main"]
    assert wf.crons == ["0 0 * * *"]
    assert parse_workflow("name: x\njobs: {}\n", "a.yml").events == []
    assert parse_workflow("on: 5\njobs: {}\n", "a.yml").events == []


def test_parse_errors() -> None:
    wf = parse_workflow("on: [push\n", "bad.yml")
    assert wf.parse_error
    assert "YAML" in wf.parse_error
    assert (
        parse_workflow("- just\n- a list\n", "bad.yml").parse_error == "workflow is not a mapping"
    )
    wf = parse_workflow("on: push\njobs: notamapping\n", "x.yml")
    assert wf.jobs == {}


def test_job_fields() -> None:
    text = """
name: CI
on: push
jobs:
  test:
    name: Test ${{ matrix.os }}
    runs-on: ${{ matrix.os }}
    timeout-minutes: "15"
    needs: lint
    if: github.event_name == 'push'
    continue-on-error: true
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, windows-latest]
        py: ["3.11", "3.12"]
        include:
          - os: macos-latest
            py: "3.12"
        exclude:
          - os: windows-latest
            py: "3.11"
    services:
      db:
        image: postgres
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - name: Run tests
        run: |
          pytest
          echo done
      - uses: ./local-action
      - uses: docker://alpine:3
      - uses: actions/cache/restore@v4
  lint:
    runs-on: [self-hosted, linux]
    needs: [a, b]
    timeout-minutes: ${{ vars.T }}
    steps: []
  grp:
    runs-on:
      group: big
      labels: [ubuntu-latest-16-cores]
    timeout-minutes: 1.5
"""
    wf = parse_workflow(text, ".github/workflows/ci.yml")
    assert wf.name == "CI"
    t = wf.jobs["test"]
    assert t.runs_on == ["ubuntu-latest", "windows-latest"]
    assert t.timeout_minutes == 15
    assert t.needs == ["lint"]
    assert t.fail_fast is False
    assert t.matrix_legs == 4 - 1 + 1
    assert t.services
    assert t.line == 5
    assert t.steps[0].action == "actions/checkout"
    assert t.steps[0].with_ == {"fetch-depth": 0}
    assert t.steps[1].display_name == "Run tests"
    assert t.steps[1].run
    assert t.steps[1].run.startswith("pytest")
    assert t.steps[2].action == "./local-action"
    assert t.steps[3].action == "docker://alpine:3"
    assert t.steps[4].action == "actions/cache/restore"
    assert t.steps[4].action_repo == "actions/cache"
    assert t.api_name_matches("Test ubuntu-latest")
    assert not t.api_name_matches("Lint")
    lint = wf.jobs["lint"]
    assert lint.runs_on == ["self-hosted", "linux"]
    assert lint.needs == ["a", "b"]
    assert lint.timeout_minutes == -1
    assert lint.api_name_matches("lint")
    assert lint.api_name_matches("lint (3.11)")
    assert not lint.api_name_matches("linter")
    grp = wf.jobs["grp"]
    assert grp.runs_on == ["ubuntu-latest-16-cores"]
    assert grp.timeout_minutes == 1
    assert wf.job_for_api_name("Test windows-latest") is t
    assert wf.job_for_api_name("lint (3.12)") is lint
    assert wf.job_for_api_name("nope") is None


def test_named_job_matching() -> None:
    wf = parse_workflow(
        "on: push\njobs:\n  a:\n    name: Unit tests\n    runs-on: ubuntu-latest\n", "x.yml"
    )
    j = wf.jobs["a"]
    assert j.api_name_matches("Unit tests")
    assert j.api_name_matches("Unit tests (3.12)")
    assert not j.api_name_matches("Unit tests-extra")
    assert wf.job_for_api_name("Unit tests") is j
    assert j.display() == "a"
    assert not j.is_reusable_call


def test_matrix_leg_count() -> None:
    assert matrix_leg_count(None) is None
    assert matrix_leg_count("${{ fromJson(needs.x.outputs.m) }}") is None
    assert matrix_leg_count({"os": "${{ fromJson('[]') }}"}) is None
    assert matrix_leg_count({"os": ["a", "b"], "py": ["1", "2", "3"]}) == 6
    assert matrix_leg_count({"include": [{"a": 1}, {"a": 2}]}) == 2
    assert matrix_leg_count({"include": "${{ x }}"}) is None
    assert matrix_leg_count({"os": ["a"], "exclude": [{"os": "a"}]}) == 0
    assert matrix_leg_count({}) is None


def test_step_display_and_reusable() -> None:
    s = Step(index=0, name=None, uses="org/act/sub@v1", run=None, with_={}, if_=None)
    assert s.action == "org/act/sub"
    assert s.action_repo == "org/act"
    assert s.display_name == "Run org/act"
    s2 = Step(index=3, name=None, uses=None, run=None, with_={}, if_=None)
    assert s2.display_name == "step 3"
    assert s2.action is None
    assert s2.action_repo is None
    wf = parse_workflow(
        "on: workflow_call\njobs:\n  a:\n    uses: o/r/.github/workflows/x.yml@main\n", "x.yml"
    )
    assert wf.is_reusable
    assert wf.jobs["a"].is_reusable_call
    assert wf.jobs["a"].runs_on == []


def test_load_from_directory(tmp_path: Path) -> None:
    assert workflow_files(tmp_path) == []
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "a.yml").write_text("on: push\njobs: {}\n", encoding="utf-8")
    (d / "b.yaml").write_text("on: push\njobs: {}\n", encoding="utf-8")
    (d / "README.md").write_text("no", encoding="utf-8")
    wfs = load_workflows(tmp_path)
    assert [w.path for w in wfs] == [".github/workflows/a.yml", ".github/workflows/b.yaml"]
    wfs2 = load_workflow_texts([("x.yml", "on: push\njobs: {}\n")])
    assert wfs2[0].path == "x.yml"


def test_job_lines_survive_bad_yaml() -> None:
    wf = parse_workflow("jobs:\n  a:\n    runs-on: x\n    timeout-minutes: true\n", "x.yml")
    assert wf.jobs["a"].line == 2
    assert wf.jobs["a"].timeout_minutes is None
    assert wf.jobs["a"].runs_on_has_expression is False
    j = Job(
        key="k",
        name=None,
        runs_on=["${{ matrix.os }}"],
        runs_on_raw=None,
        timeout_minutes=None,
        steps=[],
        needs=[],
        if_=None,
        uses=None,
        matrix=None,
        matrix_legs=None,
        fail_fast=None,
        continue_on_error=None,
        concurrency=None,
        services={},
        container=None,
        line=None,
    )
    assert j.runs_on_has_expression
