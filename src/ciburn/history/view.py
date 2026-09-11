"""HistoryView: typed, priced, in-memory view of one repository's run history."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from ciburn.history.store import Store
from ciburn.pricing import JobPrice, PriceModel, Pricing, RunnerClass, price_job
from ciburn.repo_layout import RepoLayout
from ciburn.workflow import Job, Workflow

MATRIX_SUFFIX_RE = re.compile(r"\s*\((?:[^()]*|\([^()]*\))*\)\s*$")
TERMINAL_STATUSES = {"completed"}


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def base_job_name(name: str) -> str:
    """``test (ubuntu-latest, 3.11)`` -> ``test``."""
    return MATRIX_SUFFIX_RE.sub("", name).strip() or name


@dataclass
class StepRecord:
    number: int
    name: str
    status: str | None
    conclusion: str | None
    started_at: datetime | None
    completed_at: datetime | None

    @property
    def seconds(self) -> float:
        if self.started_at and self.completed_at:
            return max(0.0, (self.completed_at - self.started_at).total_seconds())
        return 0.0


@dataclass
class JobRecord:
    id: int
    run_id: int
    run_attempt: int
    name: str
    base_name: str
    status: str | None
    conclusion: str | None
    created_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    labels: list[str]
    runner_name: str | None
    runner_group_name: str | None
    workflow_path: str
    workflow_name: str | None
    event: str | None
    head_sha: str | None
    head_branch: str | None
    run_created_at: datetime | None
    run_conclusion: str | None
    run_attempts_total: int
    runner: RunnerClass
    price: JobPrice
    steps: list[StepRecord] = field(default_factory=list)
    config_job: Job | None = None

    @property
    def raw_seconds(self) -> float:
        return self.price.raw_seconds

    @property
    def billed_minutes(self) -> int:
        return self.price.billed_minutes

    @property
    def cost(self) -> Decimal:
        return self.price.cost

    @property
    def queue_seconds(self) -> float:
        if self.created_at and self.started_at:
            return max(0.0, (self.started_at - self.created_at).total_seconds())
        return 0.0

    @property
    def failed(self) -> bool:
        return self.conclusion in ("failure", "timed_out")

    @property
    def group_key(self) -> tuple[str, str]:
        return (self.workflow_path, self.base_name)

    def step_seconds_matching(self, pattern: re.Pattern[str]) -> float:
        return sum(s.seconds for s in self.steps if pattern.search(s.name or ""))


@dataclass
class RunRecord:
    id: int
    workflow_id: int | None
    path: str
    name: str | None
    event: str | None
    status: str | None
    conclusion: str | None
    run_attempt: int
    head_sha: str | None
    head_branch: str | None
    created_at: datetime | None
    run_started_at: datetime | None
    updated_at: datetime | None
    jobs: list[JobRecord] = field(default_factory=list)
    jobs_fetched: bool = False

    @property
    def billed_minutes(self) -> int:
        return sum(j.billed_minutes for j in self.jobs)

    @property
    def cost(self) -> Decimal:
        return sum((j.cost for j in self.jobs), Decimal(0))

    @property
    def raw_seconds(self) -> float:
        return sum(j.raw_seconds for j in self.jobs)

    @property
    def failed(self) -> bool:
        return self.conclusion in ("failure", "timed_out")

    @property
    def wall_seconds(self) -> float | None:
        if self.run_started_at and self.updated_at:
            return max(0.0, (self.updated_at - self.run_started_at).total_seconds())
        return None


@dataclass
class Totals:
    runs: int = 0
    runs_with_jobs: int = 0
    jobs: int = 0
    billed_minutes: int = 0
    raw_seconds: float = 0.0
    rounding_waste_seconds: float = 0.0
    cost: Decimal = Decimal(0)
    priced_minutes: int = 0
    unpriced_hosted_minutes: int = 0
    self_hosted_minutes: int = 0
    queue_seconds: float = 0.0
    by_sku: dict[str, tuple[int, Decimal]] = field(default_factory=dict)
    by_workflow: dict[str, tuple[int, Decimal]] = field(default_factory=dict)
    by_event: dict[str, tuple[int, Decimal]] = field(default_factory=dict)
    by_conclusion: dict[str, tuple[int, Decimal]] = field(default_factory=dict)


@dataclass
class HistoryView:
    full_name: str
    private: bool
    default_branch: str
    model: PriceModel
    since: datetime
    until: datetime
    window_days: int
    runs: list[RunRecord]
    jobs: list[JobRecord]
    commit_files: Mapping[str, list[str]] = field(default_factory=dict)
    layout: RepoLayout | None = None
    timings: Mapping[int, dict[str, Any]] = field(default_factory=dict)
    last_ingest: dict[str, Any] = field(default_factory=dict)
    jobs_sampled: bool = False

    # -- construction --------------------------------------------------------------------

    @classmethod
    def from_store(
        cls,
        store: Store,
        full_name: str,
        pricing: Pricing,
        model: PriceModel,
        *,
        workflows: Iterable[Workflow] = (),
        window_days: int = 90,
        now: datetime | None = None,
        public_repo: bool | None = None,
        label_overrides: Mapping[str, str] | None = None,
    ) -> HistoryView:
        repo = store.get_repo(full_name)
        if repo is None:
            raise KeyError(f"{full_name} is not in the history cache")
        repo_id = int(repo["id"])
        private = bool(repo["private"])
        if public_repo is None:
            public_repo = not private
        now = now or datetime.now(UTC)
        since = now - timedelta(days=window_days)
        since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        wf_by_path = {w.path: w for w in workflows}

        run_rows = store.runs(repo_id, since_iso)
        runs: dict[int, RunRecord] = {}
        for r in run_rows:
            runs[int(r["id"])] = RunRecord(
                id=int(r["id"]),
                workflow_id=r["workflow_id"],
                path=str(r["path"] or ""),
                name=r["name"],
                event=r["event"],
                status=r["status"],
                conclusion=r["conclusion"],
                run_attempt=int(r["run_attempt"] or 1),
                head_sha=r["head_sha"],
                head_branch=r["head_branch"],
                created_at=parse_ts(r["created_at"]),
                run_started_at=parse_ts(r["run_started_at"]),
                updated_at=parse_ts(r["updated_at"]),
                jobs_fetched=r["jobs_fetched_at"] is not None,
            )
        job_rows = list(store.jobs_for_runs(runs.keys()))
        steps = store.steps_for_jobs(int(j["id"]) for j in job_rows)
        jobs: list[JobRecord] = []
        for j in job_rows:
            run = runs[int(j["run_id"])]
            labels = list(json.loads(j["labels"] or "[]"))
            runner = pricing.classify(labels, j["runner_name"], label_overrides)
            started, completed = parse_ts(j["started_at"]), parse_ts(j["completed_at"])
            seconds = (completed - started).total_seconds() if started and completed else 0.0
            if j["status"] != "completed" or (
                j["conclusion"] in ("skipped", "cancelled") and seconds <= 0
            ):
                seconds = max(seconds, 0.0)
            price = price_job(model, runner, seconds, public_repo=False)
            # public repos are priced at the list-price equivalent; the report labels it.
            name = str(j["name"] or "")
            wf = wf_by_path.get(run.path)
            rec = JobRecord(
                id=int(j["id"]),
                run_id=run.id,
                run_attempt=int(j["run_attempt"] or 1),
                name=name,
                base_name=base_job_name(name),
                status=j["status"],
                conclusion=j["conclusion"],
                created_at=parse_ts(j["created_at"]),
                started_at=started,
                completed_at=completed,
                labels=labels,
                runner_name=j["runner_name"],
                runner_group_name=j["runner_group_name"],
                workflow_path=run.path,
                workflow_name=j["workflow_name"] or run.name,
                event=run.event,
                head_sha=run.head_sha,
                head_branch=run.head_branch,
                run_created_at=run.created_at,
                run_conclusion=run.conclusion,
                run_attempts_total=run.run_attempt,
                runner=runner,
                price=price,
                steps=[
                    StepRecord(
                        int(s["number"]),
                        str(s["name"] or ""),
                        s["status"],
                        s["conclusion"],
                        parse_ts(s["started_at"]),
                        parse_ts(s["completed_at"]),
                    )
                    for s in steps.get(int(j["id"]), [])
                ],
                config_job=wf.job_for_api_name(name) if wf else None,
            )
            run.jobs.append(rec)
            jobs.append(rec)
        layout_state = store.get_state(repo_id, "layout")
        layout = None
        if isinstance(layout_state, dict):
            layout = RepoLayout(
                top_level_dirs=set(layout_state.get("top_level_dirs", [])),
                markdown_files=int(layout_state.get("markdown_files", 0)),
                total_files=int(layout_state.get("total_files", 0)),
            )
        fetched = sum(1 for r in runs.values() if r.jobs_fetched)
        return cls(
            full_name=str(repo["full_name"]),
            private=private,
            default_branch=str(repo["default_branch"] or "main"),
            model=model,
            since=since,
            until=now,
            window_days=window_days,
            runs=sorted(
                runs.values(),
                key=lambda r: (r.created_at or datetime.min.replace(tzinfo=UTC), r.id),
            ),
            jobs=jobs,
            commit_files=store.commit_files(repo_id),
            layout=layout,
            timings=store.timings(repo_id),
            last_ingest=store.get_state(repo_id, "last_ingest", {}) or {},
            jobs_sampled=fetched < len(runs),
        )

    # -- queries ----------------------------------------------------------------------------

    @property
    def public_repo(self) -> bool:
        return not self.private

    def runs_for(self, workflow_path: str) -> list[RunRecord]:
        return [r for r in self.runs if r.path == workflow_path]

    def jobs_for(self, workflow_path: str, job: Job | None = None) -> list[JobRecord]:
        out = [j for j in self.jobs if j.workflow_path == workflow_path]
        if job is not None:
            out = [j for j in out if j.config_job is job or job.api_name_matches(j.name)]
        return out

    def job_groups(self) -> dict[tuple[str, str], list[JobRecord]]:
        groups: dict[tuple[str, str], list[JobRecord]] = defaultdict(list)
        for j in self.jobs:
            groups[j.group_key].append(j)
        return dict(groups)

    def completed_jobs(self) -> list[JobRecord]:
        return [j for j in self.jobs if j.status == "completed" and j.raw_seconds > 0]

    def totals(self) -> Totals:
        t = Totals(runs=len(self.runs), runs_with_jobs=sum(1 for r in self.runs if r.jobs_fetched))
        for j in self.jobs:
            t.jobs += 1
            t.billed_minutes += j.billed_minutes
            t.raw_seconds += j.raw_seconds
            t.rounding_waste_seconds += j.price.rounding_waste_seconds
            t.cost += j.cost
            t.queue_seconds += j.queue_seconds
            if j.runner.kind == "hosted" and j.runner.sku is not None:
                t.priced_minutes += j.billed_minutes
                _add(t.by_sku, j.runner.sku, j.billed_minutes, j.cost)
            elif j.runner.kind == "hosted":
                t.unpriced_hosted_minutes += j.billed_minutes
                _add(t.by_sku, f"unknown:{j.runner.label or '?'}", j.billed_minutes, j.cost)
            else:
                t.self_hosted_minutes += j.billed_minutes
                _add(t.by_sku, f"self-hosted:{j.runner.label or '?'}", j.billed_minutes, j.cost)
            _add(t.by_workflow, j.workflow_path, j.billed_minutes, j.cost)
            _add(t.by_event, j.event or "?", j.billed_minutes, j.cost)
            _add(t.by_conclusion, j.conclusion or j.status or "?", j.billed_minutes, j.cost)
        return t

    def reprice(
        self, pricing: Pricing, model: PriceModel, label_overrides: Mapping[str, str] | None = None
    ) -> HistoryView:
        """Same history under another model (for `price --compare`)."""
        for j in self.jobs:
            j.runner = pricing.classify(j.labels, j.runner_name, label_overrides)
            j.price = price_job(model, j.runner, j.raw_seconds, public_repo=False)
        self.model = model
        return self


def _add(d: dict[str, tuple[int, Decimal]], key: str, minutes: int, cost: Decimal) -> None:
    m, c = d.get(key, (0, Decimal(0)))
    d[key] = (m + minutes, c + cost)
