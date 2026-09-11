"""Corpus scan: fetch a stratified sample of public repositories' Actions history.

Reproducible sample construction (see REPORT.md, "Sample construction"):

* 10 languages x 4 star buckets = 40 cells
* each cell: the top ``--per-cell`` repositories by stars from
  ``GET /search/repositories`` with
  ``language:<L> stars:<lo>..<hi> pushed:>=<PUSHED_SINCE> archived:false fork:false``,
  ``sort=stars&order=desc`` (deterministic on a given day; the selection date and
  every query string are written to ``<workdir>/sample.json``)
* per repository (``IngestOptions`` below): repo metadata, workflow list, workflow
  file contents, file tree (for layout), up to 2 pages (200) of runs created in the
  last 90 days plus the API's ``total_count`` for the window, and the jobs of up to
  8 runs (newest completed run of each workflow first, then newest overall)

Everything is checkpointed in a SQLite file and in ``sample.json``; re-running
resumes where it stopped. Only public repositories are read, only through the
REST API, only aggregate data is published.

Usage:
    GITHUB_TOKEN=... python scripts/corpus_scan.py --workdir corpus [--per-cell 50] [--max-repos N]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ciburn.history.client import GitHubClient, GitHubError, NotFoundError, token_from_env
from ciburn.history.ingest import IngestOptions, ingest
from ciburn.history.store import Store

LANGUAGES = ["Python", "JavaScript", "TypeScript", "Go", "Rust", "Java", "C++", "C#", "Ruby", "PHP"]
STAR_BUCKETS = [(100, 499), (500, 1999), (2000, 9999), (10000, 1_000_000)]
PUSHED_SINCE = "2026-06-01"
WINDOW_DAYS = 90
INGEST_OPTIONS = IngestOptions(
    days=WINDOW_DAYS,
    max_run_pages=2,
    max_job_runs=8,
    fetch_commits=False,
    fetch_layout=True,
    fetch_workflow_files=True,
    fetch_timing=False,
)
MIN_REMAINING = 25


def log(msg: str) -> None:
    print(f"{datetime.now(UTC).strftime('%H:%M:%S')} {msg}", flush=True)


def build_sample(client: GitHubClient, per_cell: int, path: Path) -> list[dict[str, Any]]:
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        log(
            f"sample: loaded {len(data['repos'])} repos from {path} (selected {data['selected_at']})"
        )
        return list(data["repos"])
    repos: list[dict[str, Any]] = []
    queries: list[str] = []
    seen: set[str] = set()
    for lang in LANGUAGES:
        for lo, hi in STAR_BUCKETS:
            q = f"language:{lang} stars:{lo}..{hi} pushed:>={PUSHED_SINCE} archived:false fork:false"
            queries.append(q)
            data = client.get_json(
                "/search/repositories",
                {"q": q, "sort": "stars", "order": "desc", "per_page": min(per_cell, 100)},
            )
            items = data.get("items", [])[:per_cell]
            for rank, it in enumerate(items, 1):
                name = str(it["full_name"])
                if name in seen:
                    continue
                seen.add(name)
                repos.append(
                    {
                        "full_name": name,
                        "id": int(it["id"]),
                        "language": lang,
                        "star_bucket": f"{lo}-{hi if hi < 1_000_000 else 'inf'}",
                        "stars_at_selection": int(it["stargazers_count"]),
                        "rank_in_cell": rank,
                        "query": q,
                    }
                )
            log(
                f"sample: {lang:11} {lo:>6}-{hi:<7} -> {len(items):3} repos (total_count {data.get('total_count')})"
            )
            time.sleep(2.2)  # search API: 30 requests/minute
    out = {
        "selected_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "per_cell": per_cell,
        "languages": LANGUAGES,
        "star_buckets": [list(b) for b in STAR_BUCKETS],
        "pushed_since": PUSHED_SINCE,
        "window_days": WINDOW_DAYS,
        "ingest_options": {
            "max_run_pages": INGEST_OPTIONS.max_run_pages,
            "max_job_runs": INGEST_OPTIONS.max_job_runs,
        },
        "queries": queries,
        "repos": repos,
    }
    path.write_text(json.dumps(out, indent=1), encoding="utf-8")
    log(f"sample: selected {len(repos)} repos -> {path}")
    return repos


def wait_for_quota(client: GitHubClient) -> None:
    rem, reset = client.rate.remaining, client.rate.reset_epoch
    if rem is not None and rem < MIN_REMAINING and reset:
        wait = max(5.0, reset - time.time() + 5)
        log(f"quota: {rem} left; sleeping {wait:.0f}s until reset")
        time.sleep(wait)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="corpus")
    ap.add_argument("--per-cell", type=int, default=50)
    ap.add_argument("--max-repos", type=int, default=None)
    ap.add_argument("--only", default=None, help="comma-separated full_names to scan (debug)")
    args = ap.parse_args()

    token = token_from_env()
    if not token:
        print("GITHUB_TOKEN is required for the corpus scan", file=sys.stderr)
        return 2
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    store = Store(workdir / "corpus.sqlite")
    client = GitHubClient(token, log=log)
    sample = build_sample(client, args.per_cell, workdir / "sample.json")
    if args.only:
        wanted = set(args.only.split(","))
        sample = [r for r in sample if r["full_name"] in wanted]
    if args.max_repos:
        sample = sample[: args.max_repos]

    done = errors = 0
    started = time.time()
    for i, entry in enumerate(sample, 1):
        name = entry["full_name"]
        owner, repo = name.split("/", 1)
        prior = store.get_repo(name)
        status = store.get_state(int(prior["id"]), "corpus_status") if prior else None
        if status and status.get("status") in ("done", "gone"):
            continue
        wait_for_quota(client)
        t0 = time.time()
        try:
            res = ingest(client, store, owner, repo, INGEST_OPTIONS)
        except NotFoundError:
            log(f"[{i}/{len(sample)}] {name}: gone (404)")
            rid = store.upsert_repo({"id": entry["id"], "full_name": name, "private": False})
            store.set_state(rid, "corpus_status", {"status": "gone"})
            continue
        except GitHubError as exc:
            errors += 1
            log(f"[{i}/{len(sample)}] {name}: ERROR {exc}")
            if prior:
                store.set_state(
                    int(prior["id"]), "corpus_status", {"status": "error", "error": str(exc)[:200]}
                )
            if errors > 50:
                log("too many errors; stopping")
                return 1
            continue
        store.set_state(
            res.repo_id,
            "corpus_status",
            {
                "status": "done",
                "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "requests": res.requests,
                "sample": {
                    k: entry[k]
                    for k in ("language", "star_bucket", "stars_at_selection", "rank_in_cell")
                },
                "warnings": res.warnings[:5],
            },
        )
        done += 1
        elapsed = time.time() - started
        log(
            f"[{i}/{len(sample)}] {name}: runs {res.runs_seen}/{res.runs_total_in_window} jobs {res.jobs_seen} "
            f"wf {res.workflow_files} req {res.requests} ({time.time() - t0:.1f}s) "
            f"remaining {client.rate.remaining} | done {done} in {elapsed / 60:.0f} min"
        )
        (workdir / "progress.json").write_text(
            json.dumps(
                {
                    "index": i,
                    "total": len(sample),
                    "done_this_session": done,
                    "errors": errors,
                    "requests": client.rate.requests_made,
                    "slept": client.rate.seconds_slept,
                    "at": datetime.now(UTC).isoformat(),
                }
            ),
            encoding="utf-8",
        )
    log(
        f"finished: {done} scanned this session, {errors} errors, {client.rate.requests_made} requests"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
