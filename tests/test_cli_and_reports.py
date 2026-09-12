from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from rich.console import Console

from ciburn.audit import AuditOptions, AuditResult, detect_repo, parse_label_overrides, run_audit
from ciburn.cli import cli
from ciburn.pricing import Pricing
from ciburn.report import render_html, render_json, render_markdown, render_terminal
from synth import NOW, Synth
from test_join_and_history_rules import CI_YML, PATH, build_rich_history

FIX_YML = """name: ci
on:
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: pytest
"""


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    d = tmp_path / "repo"
    (d / ".github" / "workflows").mkdir(parents=True)
    (d / ".github" / "workflows" / "ci.yml").write_text(CI_YML, encoding="utf-8")
    (d / "docs").mkdir()
    (d / "docs" / "a.md").write_text("x", encoding="utf-8")
    (d / "README.md").write_text("x", encoding="utf-8")
    (d / "CHANGELOG.md").write_text("x", encoding="utf-8")
    return d


@pytest.fixture
def cache_with_history(tmp_path: Path, pricing: Pricing) -> Path:
    """A cache file pre-populated with synthetic history for acme/widgets."""
    s = Synth()
    build_rich_history(s)
    from ciburn.workflow import parse_workflow

    s.view(pricing, [parse_workflow(CI_YML, PATH)])  # flushes runs/jobs into s.store
    path = tmp_path / "cache.sqlite"
    s.store.upsert_workflow_file(s.repo_id, PATH, "main", "sha", CI_YML)
    s.store.set_state(
        s.repo_id,
        "layout",
        {"top_level_dirs": ["docs", "src"], "markdown_files": 4, "total_files": 10},
    )
    dst = __import__("sqlite3").connect(str(path))
    s.store.conn.backup(dst)
    dst.close()
    return path


def test_run_audit_static_only(repo_dir: Path) -> None:
    res = run_audit(AuditOptions(path=repo_dir, no_history=True, now=NOW))
    assert res.history is None
    assert res.advisory
    assert not res.measured
    assert res.worst_severity() is not None
    d = res.as_dict()
    assert d["history"] is None
    assert d["summary"]["measured"] == 0
    assert d["findings"][0]["kind"] == "advisory"


def test_run_audit_offline_with_cached_history(repo_dir: Path, cache_with_history: Path) -> None:
    res = run_audit(
        AuditOptions(
            path=repo_dir,
            repo="acme/widgets",
            cache=cache_with_history,
            offline=True,
            now=NOW,
            plan="team",
        )
    )
    assert res.history is not None
    assert res.measured
    assert res.totals is not None
    assert res.totals.billed_minutes > 0
    assert res.included is not None
    assert any("Public repository" in n for n in res.notes)
    assert res.recoverable_cost_total() > 0
    d = res.as_dict()
    assert d["totals"]["billed_minutes"] == res.totals.billed_minutes
    assert d["summary"]["recoverable_cost_est_capped"] <= d["totals"]["list_price_cost"]
    assert res.recoverable_minutes_capped() <= res.totals.billed_minutes
    assert d["included_minutes"]["plan"] == "team"
    assert d["pricing"]["public_repo_list_price_equivalent"] is True
    # workflows can also come from the cache when no path is given
    res2 = run_audit(
        AuditOptions(repo="acme/widgets", cache=cache_with_history, offline=True, now=NOW)
    )
    assert res2.workflows
    assert res2.history is not None
    assert res2.history.layout is not None


def test_run_audit_offline_without_cache_reports_error(repo_dir: Path, tmp_path: Path) -> None:
    res = run_audit(
        AuditOptions(
            path=repo_dir,
            repo="acme/widgets",
            cache=tmp_path / "empty.sqlite",
            offline=True,
            now=NOW,
        )
    )
    assert res.error is not None
    assert "No cached history" in res.error
    assert res.advisory


def test_run_audit_scenario_model_note(repo_dir: Path) -> None:
    res = run_audit(
        AuditOptions(path=repo_dir, no_history=True, model_id="2026-selfhosted-announced", now=NOW)
    )
    assert any("not in effect" in n for n in res.notes)


def test_reporters(repo_dir: Path, cache_with_history: Path) -> None:
    res = run_audit(
        AuditOptions(
            path=repo_dir, repo="acme/widgets", cache=cache_with_history, offline=True, now=NOW
        )
    )
    md = render_markdown(res)
    assert "### Measured findings" in md
    assert "### Advisory findings" in md
    assert "Where the minutes go" in md
    html = render_html(res)
    assert "<table class='measured'>" in html
    assert "ciburn report" in html
    j = json.loads(render_json(res))
    assert j["schema_version"] == 1
    assert j["findings"]
    with (Path(cache_with_history).parent / "t.txt").open("w", encoding="utf-8") as fh:
        console = Console(record=True, width=120, file=fh)
        render_terminal(res, console)
        text = console.export_text()
    assert "MEASURED" in text
    assert "ADVISORY" in text
    # empty result rendering
    empty = AuditResult(
        generated_at=NOW,
        pricing=res.pricing,
        model=res.model,
        window_days=30,
        workflows=[],
        findings=[],
        error="boom",
        notes=["n1"],
    )
    assert "boom" in render_markdown(empty)
    assert "boom" in render_html(empty)
    with (Path(cache_with_history).parent / "t2.txt").open("w", encoding="utf-8") as fh2:
        c2 = Console(record=True, width=100, file=fh2)
        render_terminal(empty, c2)
        assert "no findings" in c2.export_text()


def test_terminal_layout_follows_console_width(repo_dir: Path, cache_with_history: Path) -> None:
    measured = run_audit(
        AuditOptions(
            path=repo_dir, repo="acme/widgets", cache=cache_with_history, offline=True, now=NOW
        )
    )
    assert measured.measured
    advisory = run_audit(AuditOptions(path=repo_dir, no_history=True, now=NOW))
    assert advisory.advisory

    def render(res: AuditResult, width: int) -> str:
        console = Console(record=True, width=width, file=io.StringIO())
        render_terminal(res, console)
        return console.export_text()

    # 80 (the macOS Terminal default) up to 119: one block per finding, wrapped to the console
    for width in (80, 100, 119):
        text = render(measured, width)
        assert "observed: " in text
        assert "recoverable: " in text
        assert "confidence " in text
        assert "recoverable (estimate)" not in text
        assert all(len(line) <= width for line in text.splitlines()), width
        for f in measured.measured:
            assert f.message.split()[0] in text
        text = render(advisory, width)
        assert "ADVISORY — configuration only" in text
        assert "fix: " in text
        assert all(len(line) <= width for line in text.splitlines()), width
    # 120 and wider: the tables, with the finding column wide enough to read
    for width in (120, 140):
        text = render(measured, width)
        assert "(estimate)" in text  # the table header, possibly wrapped
        assert "observed: " not in text
        assert "fix: " in text
        assert all(len(line) <= width for line in text.splitlines()), width
        text = render(advisory, width)
        assert "ADVISORY — configuration only" in text
        assert all(len(line) <= width for line in text.splitlines()), width


def test_progress_clears_transient_line_before_permanent_line() -> None:
    from ciburn.cli import _progress

    buf = io.StringIO()
    cb = _progress(False, Console(file=buf, force_terminal=True, color_system=None, width=80))
    cb("repo", {"full_name": "acme/widgets", "private": False})
    cb("runs", {"seen": 100})
    cb("runs", {"seen": 62})
    cb("jobs", {"done": 40, "total": 62, "jobs": 148})
    cb("done", {"requests": 50})
    raw = buf.getvalue()
    # replay the carriage returns the way a terminal would
    screen: list[str] = []
    for line in raw.split("\n"):
        row = ""
        for seg in line.split("\r"):
            row = seg + row[len(seg) :]
        screen.append(row.rstrip())
    assert screen[:3] == ["fetching history for acme/widgets…", "  done (50 API requests)", ""]
    assert "obs)" not in raw.replace("(148 jobs)", "")

    # not a terminal (CI logs, pipes): no in-place counters at all
    buf = io.StringIO()
    cb = _progress(False, Console(file=buf, force_terminal=False, width=80))
    cb("runs", {"seen": 100})
    cb("jobs", {"done": 40, "total": 62, "jobs": 148})
    cb("done", {"requests": 3})
    assert buf.getvalue() == "  done (3 API requests)\n"

    buf = io.StringIO()
    cb = _progress(True, Console(file=buf, force_terminal=True, width=80))
    cb("done", {"requests": 3})
    assert buf.getvalue() == ""


def test_cli_audit_static_and_formats(repo_dir: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    r = runner.invoke(cli, ["audit", "--path", str(repo_dir), "--no-history"])
    assert r.exit_code == 0, r.output
    assert "ADVISORY" in r.output
    r = runner.invoke(cli, ["audit", "--path", str(repo_dir), "--no-history", "--fail-on", "low"])
    assert r.exit_code == 1
    r = runner.invoke(cli, ["audit", "--path", str(repo_dir), "--no-history", "--format", "json"])
    assert r.exit_code == 0
    assert json.loads(r.output)["summary"]["advisory"] > 0
    out = tmp_path / "rep.md"
    r = runner.invoke(
        cli, ["audit", "--path", str(repo_dir), "--no-history", "-f", "md", "-o", str(out)]
    )
    assert r.exit_code == 0
    assert out.read_text(encoding="utf-8").startswith("## ciburn")
    out2 = tmp_path / "rep.txt"
    r = runner.invoke(cli, ["audit", "--path", str(repo_dir), "--no-history", "-o", str(out2)])
    assert "ADVISORY" in out2.read_text(encoding="utf-8")
    r = runner.invoke(cli, ["audit", "--path", str(repo_dir), "--no-history", "-f", "html"])
    assert "<table" in r.output
    r = runner.invoke(cli, ["audit", "--path", str(tmp_path), "--no-history"])
    assert r.exit_code == 2
    assert "no .github/workflows" in r.output
    r = runner.invoke(cli, ["audit", "--path", str(repo_dir), "--no-history", "--label-sku", "bad"])
    assert r.exit_code == 2
    r = runner.invoke(
        cli,
        ["audit", "--path", str(repo_dir), "--no-history", "-f", "json", "--ignore", "W003,W001"],
    )
    assert {f["rule_id"] for f in json.loads(r.output)["findings"]}.isdisjoint({"W003", "W001"})


def test_cli_audit_offline_history_and_price(repo_dir: Path, cache_with_history: Path) -> None:
    runner = CliRunner()
    args = [
        "--path",
        str(repo_dir),
        "--repo",
        "acme/widgets",
        "--cache",
        str(cache_with_history),
        "--offline",
        "-q",
    ]
    r = runner.invoke(cli, ["audit", *args, "--fail-on", "high"])
    assert r.exit_code == 1, r.output  # H001 high
    assert "MEASURED" in r.output
    r = runner.invoke(cli, ["price", *args, "--compare"])
    assert r.exit_code == 0, r.output
    assert "2026 vs 2025" in r.output
    r = runner.invoke(cli, ["price", *args, "--format", "json", "--plan", "free"])
    assert r.exit_code == 0
    data = json.loads(r.output)
    assert data["models"][0]["model"] == "2026"
    assert data["models"][0]["net_after_plan_est"] is not None
    r = runner.invoke(
        cli,
        [
            "price",
            "--path",
            str(repo_dir),
            "--repo",
            "acme/widgets",
            "--cache",
            str(cache_with_history.parent / "none.sqlite"),
            "--offline",
            "-q",
        ],
    )
    assert r.exit_code == 2


def test_cli_fix(tmp_path: Path) -> None:
    d = tmp_path / "r"
    (d / ".github" / "workflows").mkdir(parents=True)
    (d / ".github" / "workflows" / "ci.yml").write_text(FIX_YML, encoding="utf-8")
    runner = CliRunner()
    r = runner.invoke(cli, ["fix", "--path", str(d), "--no-history"])
    assert r.exit_code == 0, r.output
    assert "+concurrency:" in r.output
    assert "+    timeout-minutes: 30" in r.output
    patch = tmp_path / "p.patch"
    r = runner.invoke(
        cli, ["fix", "--path", str(d), "--no-history", "--rules", "W003", "-o", str(patch)]
    )
    assert r.exit_code == 0
    assert "timeout-minutes" in patch.read_text(encoding="utf-8")
    assert "concurrency" not in patch.read_text(encoding="utf-8")
    r = runner.invoke(cli, ["fix", "--path", str(d), "--no-history", "--write"])
    assert r.exit_code == 0
    assert "timeout-minutes: 30" in (d / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    r = runner.invoke(cli, ["fix", "--path", str(d), "--no-history"])
    assert "nothing to fix" in r.output
    assert "apply it explicitly with --rules W010" in r.output
    r = runner.invoke(cli, ["fix", "--path", str(d), "--no-history", "--rules", "W010"])
    assert "+    if: github.event.pull_request.draft == false" in r.output
    r = runner.invoke(cli, ["fix", "--path", str(tmp_path / "nowhere"), "--no-history"])
    assert r.exit_code == 2


def test_cli_rules_models_version() -> None:
    runner = CliRunner()
    assert "H001" in runner.invoke(cli, ["rules"]).output
    assert "2026-selfhosted-announced" in runner.invoke(cli, ["models"]).output
    assert "0.1.1" in runner.invoke(cli, ["--version"]).output


def test_detect_repo_and_overrides(tmp_path: Path) -> None:
    assert detect_repo(tmp_path) is None
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin", "git@github.com:acme/widgets.git"],
        check=True,
    )
    assert detect_repo(tmp_path) == "acme/widgets"
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "set-url",
            "origin",
            "https://github.com/acme/widgets",
        ],
        check=True,
    )
    assert detect_repo(tmp_path) == "acme/widgets"
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "set-url",
            "origin",
            "https://gitlab.com/acme/widgets",
        ],
        check=True,
    )
    assert detect_repo(tmp_path) is None
    assert parse_label_overrides(["a=b", " c = d "]) == {"a": "b", "c": "d"}
    assert parse_label_overrides({"x": "y"}) == {"x": "y"}
    with pytest.raises(ValueError, match="LABEL=SKU"):
        parse_label_overrides(["nope"])
