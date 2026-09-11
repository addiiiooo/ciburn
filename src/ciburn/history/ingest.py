"""Incremental, resumable ingestion of a repository's Actions history into the Store."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ciburn.history.client import GitHubClient, GitHubError, NotFoundError
from ciburn.history.store import Store

Progress = Callable[[str, dict[str, Any]], None]


def _noop(_event: str, _info: dict[str, Any]) -> None:
    return None


@dataclass
class IngestOptions:
    days: int = 90
    max_run_pages: int | None = None  # None = all pages in the window
    max_job_runs: int | None = None  # None = jobs for every run in the window
    fetch_commits: bool = True
    max_commits: int = 150
    fetch_layout: bool = True
    fetch_workflow_files: bool = True
    fetch_timing: bool = False  # only meaningful for private repos
    events_filter: list[str] = field(default_factory=list)


@dataclass
class IngestResult:
    repo_id: int
    full_name: str
    private: bool
    default_branch: str
    since: datetime
    runs_seen: int = 0
    runs_total_in_window: int = 0
    runs_jobs_fetched: int = 0
    jobs_seen: int = 0
    commits_fetched: int = 0
    workflow_files: int = 0
    requests: int = 0
    seconds_slept: float = 0.0
    warnings: list[str] = field(default_factory=list)


def ingest(
    client: GitHubClient,
    store: Store,
    owner: str,
    repo: str,
    opts: IngestOptions | None = None,
    progress: Progress = _noop,
    now: datetime | None = None,
) -> IngestResult:
    opts = opts or IngestOptions()
    now = now or datetime.now(UTC)
    since = now - timedelta(days=opts.days)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    start_requests = client.rate.requests_made

    repo_data = client.get_repo(owner, repo)
    repo_id = store.upsert_repo(repo_data)
    full_name = str(repo_data["full_name"])
    default_branch = str(repo_data.get("default_branch") or "main")
    result = IngestResult(
        repo_id=repo_id,
        full_name=full_name,
        private=bool(repo_data.get("private")),
        default_branch=default_branch,
        since=since,
    )
    progress("repo", {"full_name": full_name, "private": result.private})

    # workflows + files
    workflows = client.list_workflows(owner, repo)
    store.upsert_workflows(repo_id, workflows)
    progress("workflows", {"count": len(workflows)})
    if opts.fetch_workflow_files:
        result.workflow_files = _fetch_workflow_files(
            client, store, owner, repo, repo_id, default_branch, result
        )

    # layout (one request)
    if opts.fetch_layout:
        try:
            paths, truncated = client.get_tree_paths(owner, repo, default_branch)
            top = sorted({p.split("/", 1)[0] for p in paths if "/" in p})
            md = sum(1 for p in paths if p.lower().endswith((".md", ".mdx", ".rst")))
            store.set_state(
                repo_id,
                "layout",
                {
                    "top_level_dirs": top,
                    "markdown_files": md,
                    "total_files": len(paths),
                    "truncated": truncated,
                },
            )
        except GitHubError as exc:
            result.warnings.append(f"layout: {exc}")

    # runs
    extra = {"event": opts.events_filter[0]} if len(opts.events_filter) == 1 else None
    totals: list[int] = []

    def _on_page(page: Any) -> None:
        if not totals and isinstance(page, dict) and "total_count" in page:
            totals.append(int(page["total_count"]))

    runs_iter = client.list_runs(
        owner,
        repo,
        created_since=since.strftime("%Y-%m-%d"),
        max_pages=opts.max_run_pages,
        extra=extra,
        on_page=_on_page,
    )
    batch: list[dict[str, Any]] = []
    for run in runs_iter:
        if str(run.get("created_at", "")) < since_iso:
            continue
        batch.append(run)
        if len(batch) >= 100:
            store.upsert_runs(repo_id, batch)
            result.runs_seen += len(batch)
            progress("runs", {"seen": result.runs_seen})
            batch = []
    if batch:
        store.upsert_runs(repo_id, batch)
        result.runs_seen += len(batch)
        progress("runs", {"seen": result.runs_seen})
    result.runs_total_in_window = totals[0] if totals else result.runs_seen
    store.set_state(
        repo_id,
        "last_ingest",
        {
            "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "days": opts.days,
            "since": since_iso,
            "runs_total_in_window": result.runs_total_in_window,
            "runs_fetched": result.runs_seen,
        },
    )

    # jobs (resumable: only runs not yet fetched, or fetched while still running)
    pending = store.runs_needing_jobs(repo_id, since_iso)
    if opts.max_job_runs is not None:
        pending = _sample_runs(pending, opts.max_job_runs)
    total = len(pending)
    for i, run in enumerate(pending, 1):
        try:
            jobs = client.list_jobs(owner, repo, int(run["id"]))
        except NotFoundError:
            store.mark_jobs_fetched(int(run["id"]), "gone")
            continue
        n = store.upsert_jobs(repo_id, int(run["id"]), jobs)
        store.mark_jobs_fetched(int(run["id"]), str(run["status"]) if run["status"] else None)
        result.runs_jobs_fetched += 1
        result.jobs_seen += n
        if i % 10 == 0 or i == total:
            progress("jobs", {"done": i, "total": total, "jobs": result.jobs_seen})
        if opts.fetch_timing and result.private:
            try:
                t = client.get_run_timing(owner, repo, int(run["id"]))
                store.upsert_timing(
                    repo_id, int(run["id"]), t.get("billable") or {}, t.get("run_duration_ms")
                )
            except GitHubError as exc:
                result.warnings.append(f"timing {run['id']}: {exc}")

    # commits (files touched) for docs-only / no-change analysis
    if opts.fetch_commits:
        shas: list[str] = []
        seen: set[str] = set()
        for run in store.runs(repo_id, since_iso)[::-1]:  # newest first
            sha = run["head_sha"]
            if (
                sha
                and sha not in seen
                and run["event"] in ("push", "pull_request", "pull_request_target")
            ):
                seen.add(sha)
                if not store.has_commit(repo_id, sha):
                    shas.append(sha)
            if len(shas) >= opts.max_commits:
                break
        for k, sha in enumerate(shas, 1):
            try:
                c = client.get_commit(owner, repo, sha)
            except NotFoundError:
                store.upsert_commit(repo_id, sha, [], 0, 0)
                continue
            except GitHubError as exc:
                result.warnings.append(f"commit {sha[:7]}: {exc}")
                break
            files = [str(f.get("filename")) for f in (c.get("files") or [])]
            stats = c.get("stats") or {}
            store.upsert_commit(
                repo_id,
                sha,
                files,
                int(stats.get("additions") or 0),
                int(stats.get("deletions") or 0),
            )
            result.commits_fetched += 1
            if k % 20 == 0 or k == len(shas):
                progress("commits", {"done": k, "total": len(shas)})

    result.requests = client.rate.requests_made - start_requests
    result.seconds_slept = client.rate.seconds_slept
    progress("done", {"requests": result.requests})
    return result


def _fetch_workflow_files(
    client: GitHubClient,
    store: Store,
    owner: str,
    repo: str,
    repo_id: int,
    ref: str,
    result: IngestResult,
) -> int:
    try:
        entries = client.list_workflow_dir(owner, repo, ref)
    except GitHubError as exc:
        result.warnings.append(f"workflow files: {exc}")
        return 0
    keep: list[str] = []
    fetched = 0
    for e in entries:
        path = str(e.get("path") or "")
        if not path.endswith((".yml", ".yaml")):
            continue
        keep.append(path)
        sha = str(e.get("sha") or "")
        if sha and store.workflow_file_sha(repo_id, path) == sha:
            continue
        try:
            url = e.get("download_url")
            text = (
                client.download_text(str(url))
                if url
                else client.get_file_text(owner, repo, path, ref)
            )
        except GitHubError as exc:
            result.warnings.append(f"{path}: {exc}")
            continue
        store.upsert_workflow_file(repo_id, path, ref, sha, text)
        fetched += 1
    store.delete_workflow_files_not_in(repo_id, keep)
    return len(keep)


def _sample_runs(pending: list[Any], limit: int) -> list[Any]:
    """Deterministic sample: newest completed run of each workflow first, then newest overall."""
    if len(pending) <= limit:
        return pending
    chosen: list[Any] = []
    seen_paths: set[str] = set()
    for run in pending:  # newest first
        if run["status"] == "completed" and run["path"] not in seen_paths:
            seen_paths.add(run["path"])
            chosen.append(run)
            if len(chosen) >= limit:
                return chosen
    chosen_ids = {r["id"] for r in chosen}
    for run in pending:
        if run["id"] not in chosen_ids:
            chosen.append(run)
            if len(chosen) >= limit:
                break
    return chosen
