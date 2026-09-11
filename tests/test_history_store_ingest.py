from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ciburn.history.client import GitHubClient
from ciburn.history.ingest import IngestOptions, _sample_runs, ingest
from ciburn.history.store import Store, default_cache_path
from ciburn.history.view import HistoryView, base_job_name, parse_ts
from ciburn.pricing import Pricing
from ciburn.workflow import parse_workflow
from fake_github import FakeGitHub

NOW = datetime(2026, 9, 12, 0, 0, tzinfo=UTC)


def make(fake: FakeGitHub) -> GitHubClient:
    return GitHubClient("tok", transport=fake.transport(), sleep=lambda s: None)


def test_default_cache_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CIBURN_CACHE", str(tmp_path / "x.sqlite"))
    assert default_cache_path() == tmp_path / "x.sqlite"
    monkeypatch.delenv("CIBURN_CACHE")
    p = default_cache_path()
    assert p.name == "history.sqlite"
    assert "ciburn" in p.parts


def test_store_roundtrip(tmp_path: Path) -> None:
    with Store(tmp_path / "h.sqlite") as store:
        rid = store.upsert_repo(
            {"id": 1, "full_name": "o/r", "private": True, "default_branch": "dev"}
        )
        assert store.get_repo("O/R") is not None
        assert store.get_repo("nope") is None
        assert [r["full_name"] for r in store.list_repos()] == ["o/r"]
        store.upsert_workflows(
            rid, [{"id": 5, "name": "CI", "path": ".github/workflows/ci.yml", "state": "active"}]
        )
        assert store.workflows(rid)[0]["path"] == ".github/workflows/ci.yml"
        store.upsert_workflow_file(rid, ".github/workflows/ci.yml", "dev", "sha1", "on: push\n")
        store.upsert_workflow_file(rid, ".github/workflows/old.yml", "dev", "sha2", "on: push\n")
        assert store.workflow_file_sha(rid, ".github/workflows/ci.yml") == "sha1"
        store.delete_workflow_files_not_in(rid, [".github/workflows/ci.yml"])
        assert [p for p, _ in store.workflow_files(rid)] == [".github/workflows/ci.yml"]
        store.set_state(rid, "k", {"a": 1})
        assert store.get_state(rid, "k") == {"a": 1}
        assert store.get_state(rid, "missing", 7) == 7
        store.upsert_commit(rid, "abc", ["README.md"], 1, 2)
        assert store.has_commit(rid, "abc")
        assert store.commit_files(rid) == {"abc": ["README.md"]}
        store.upsert_timing(rid, 99, {"UBUNTU": {"total_ms": 1000}}, 5000)
        assert store.timings(rid) == {99: {"UBUNTU": {"total_ms": 1000}}}
        assert store.counts(rid)["commits"] == 1
    # reopen: data persists
    with Store(tmp_path / "h.sqlite") as store2:
        assert store2.get_repo("o/r") is not None


def test_ingest_end_to_end_and_resume() -> None:
    fake = FakeGitHub.from_fixtures()
    fake.tree = ["README.md", "docs/a.md", "src/flask/app.py"]
    fake.workflow_dir = [
        {
            "type": "file",
            "path": ".github/workflows/ci.yml",
            "sha": "s1",
            "download_url": "https://raw.example.com/.github/workflows/ci.yml",
        },
        {"type": "file", "path": ".github/workflows/notes.md", "sha": "s2", "download_url": "x"},
    ]
    fake.raw_files[".github/workflows/ci.yml"] = (
        "on: push\njobs:\n  main:\n    runs-on: ubuntu-latest\n"
    )
    sha = fake.runs[0]["head_sha"]
    fake.commits[sha] = {
        "files": [{"filename": "README.md"}],
        "stats": {"additions": 1, "deletions": 0},
    }
    events: list[str] = []
    store = Store()
    client = make(fake)
    res = ingest(
        client,
        store,
        "pallets",
        "flask",
        IngestOptions(days=90, max_commits=1),
        progress=lambda e, i: events.append(e),
        now=NOW,
    )
    assert res.full_name == "pallets/flask"
    assert res.runs_seen == 5
    assert res.runs_jobs_fetched == 1  # the other 4 runs 404 on /jobs in the fake -> marked gone
    assert res.jobs_seen == 1
    assert res.workflow_files == 1
    assert res.commits_fetched == 1
    assert "done" in events
    assert store.get_state(res.repo_id, "layout")["markdown_files"] == 2
    first_requests = len(fake.requests)

    # second ingest: no jobs re-fetched for completed runs, workflow file not re-downloaded
    res2 = ingest(client, store, "pallets", "flask", IngestOptions(days=90, max_commits=1), now=NOW)
    assert res2.runs_jobs_fetched == 0
    assert res2.commits_fetched == 0
    new = fake.requests[first_requests:]
    assert not any("/jobs" in r for r in new)
    assert not any("raw.example.com" in r for r in new)


def test_ingest_options_limits_and_warnings() -> None:
    fake = FakeGitHub.from_fixtures()
    fake.per_page_cap = 2
    store = Store()
    client = make(fake)
    res = ingest(
        client,
        store,
        "pallets",
        "flask",
        IngestOptions(
            days=90,
            max_run_pages=1,
            max_job_runs=1,
            fetch_commits=False,
            fetch_layout=True,
            fetch_workflow_files=True,
            events_filter=["push"],
        ),
        now=NOW,
    )
    assert res.runs_seen == 2
    assert res.runs_total_in_window == 5
    assert store.get_state(res.repo_id, "last_ingest")["runs_total_in_window"] == 5
    assert res.runs_jobs_fetched == 1
    assert any("event=push" in r for r in fake.requests)
    assert store.get_state(res.repo_id, "layout") == {
        "top_level_dirs": [],
        "markdown_files": 0,
        "total_files": 0,
        "truncated": False,
    }


def test_ingest_timing_for_private_repo_and_commit_404() -> None:
    fake = FakeGitHub.from_fixtures()
    fake.repo["private"] = True
    rid = int(fake.runs[0]["id"])
    fake.timing[rid] = {"billable": {"UBUNTU": {"total_ms": 60000}}, "run_duration_ms": 70000}
    store = Store()
    res = ingest(
        make(fake),
        store,
        "pallets",
        "flask",
        IngestOptions(fetch_timing=True, fetch_layout=False, fetch_workflow_files=False),
        now=NOW,
    )
    assert res.private
    assert store.timings(res.repo_id)[rid] == {"UBUNTU": {"total_ms": 60000}}
    assert res.commits_fetched == 0  # all 404 -> stored as empty
    assert store.has_commit(res.repo_id, fake.runs[0]["head_sha"])


def test_sample_runs() -> None:
    rows: list[dict[str, Any]] = [
        {"id": 1, "status": "completed", "path": "a.yml"},
        {"id": 2, "status": "completed", "path": "a.yml"},
        {"id": 3, "status": "in_progress", "path": "b.yml"},
        {"id": 4, "status": "completed", "path": "b.yml"},
        {"id": 5, "status": "completed", "path": "c.yml"},
    ]
    assert [r["id"] for r in _sample_runs(rows, 10)] == [1, 2, 3, 4, 5]
    assert [r["id"] for r in _sample_runs(rows, 2)] == [1, 4]
    assert [r["id"] for r in _sample_runs(rows, 4)] == [1, 4, 5, 2]


def test_history_view(pricing: Pricing) -> None:
    fake = FakeGitHub.from_fixtures()
    store = Store()
    res = ingest(
        make(fake),
        store,
        "pallets",
        "flask",
        IngestOptions(fetch_commits=False, fetch_layout=False, fetch_workflow_files=False),
        now=NOW,
    )
    wf = parse_workflow(
        "on: pull_request\njobs:\n  main:\n    runs-on: ubuntu-latest\n    steps: []\n",
        ".github/workflows/pre-commit.yaml",
    )
    view = HistoryView.from_store(
        store, "pallets/flask", pricing, pricing.model("2026"), workflows=[wf], now=NOW
    )
    assert view.full_name == "pallets/flask"
    assert view.public_repo
    assert len(view.runs) == 5
    assert len(view.jobs) == 1
    job = view.jobs[0]
    assert job.runner.sku == "actions_linux"
    assert job.raw_seconds == 17.0
    assert job.billed_minutes == 1
    assert job.cost == pricing.model("2026").sku("actions_linux").per_minute
    assert job.config_job is wf.jobs["main"]
    assert job.queue_seconds == 2.0
    assert len(job.steps) == 11
    assert job.step_seconds_matching(__import__("re").compile("Set up job")) >= 0
    assert view.jobs_for(".github/workflows/pre-commit.yaml", wf.jobs["main"]) == [job]
    assert view.jobs_for("nope") == []
    assert list(view.job_groups()) == [(".github/workflows/pre-commit.yaml", "main")]
    t = view.totals()
    assert t.jobs == 1
    assert t.billed_minutes == 1
    assert t.priced_minutes == 1
    assert t.by_sku["actions_linux"] == (1, job.cost)
    assert t.runs_with_jobs == 5
    assert not view.jobs_sampled
    run = view.runs_for(".github/workflows/pre-commit.yaml")[-1]
    assert run.billed_minutes == 1
    assert run.wall_seconds == 21.0
    assert not run.failed
    assert view.completed_jobs() == [job]
    old = job.cost
    view.reprice(pricing, pricing.model("2025"))
    assert view.jobs[0].cost > old
    with pytest.raises(KeyError):
        HistoryView.from_store(store, "nope/nope", pricing, pricing.model("2026"))
    assert res.repo_id


def test_helpers() -> None:
    assert base_job_name("test (ubuntu-latest, 3.11)") == "test"
    assert base_job_name("build (a (b))") == "build"
    assert base_job_name("plain") == "plain"
    assert base_job_name("()") == "()"
    assert base_job_name("weird (unclosed") == "weird (unclosed"
    assert base_job_name("a (b) c") == "a (b) c"
    import time

    t0 = time.perf_counter()
    assert base_job_name("test (" + "a" * 5000) == "test (" + "a" * 5000
    assert time.perf_counter() - t0 < 0.5
    assert parse_ts(None) is None
    assert parse_ts("garbage") is None
    assert parse_ts("2026-01-01T00:00:00Z") == datetime(2026, 1, 1, tzinfo=UTC)
