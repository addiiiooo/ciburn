"""The join: attach observed minutes, money and recoverable estimates from the
repository's own history to static (advisory) findings.

Every measurement states its method inline (``estimate_method``) and its
confidence. A finding with no matching history stays ``advisory``. Observed
values are measurements; ``recoverable_*_est`` values are always estimates.
"""

from __future__ import annotations

import itertools
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal

from ciburn.findings import Confidence, Evidence, Finding, Kind
from ciburn.history.view import HistoryView, JobRecord, RunRecord
from ciburn.pricing import billable_minutes
from ciburn.rules.base import Context
from ciburn.rules.static_jobs import INSTALL_RE
from ciburn.stats import confidence_from_count, median, percentile
from ciburn.workflow import Job, Workflow

CHECKOUT_STEP_RE = re.compile(r"checkout", re.IGNORECASE)
PR_EVENTS = ("pull_request", "pull_request_target")


@dataclass
class Measure:
    observed_minutes: float
    observed_cost: float
    observed_runs: int
    recoverable_minutes: float | None
    recoverable_cost: float | None
    method: str
    confidence: Confidence
    evidence: list[Evidence]
    note: str | None = None


Measurer = Callable[[Finding, Context, HistoryView, Workflow, Job | None], Measure | None]


def join_findings(findings: list[Finding], ctx: Context) -> list[Finding]:
    history = ctx.history
    if history is None:
        return findings
    wf_by_path = {w.path: w for w in ctx.workflows}
    out: list[Finding] = []
    findings = list(findings) + rounding_findings(findings, ctx, history)
    for f in findings:
        wf = wf_by_path.get(f.workflow or "")
        measurer = MEASURERS.get(f.rule_id)
        if wf is None or measurer is None:
            out.append(f)
            continue
        job = wf.jobs.get(f.job) if f.job else None
        m = measurer(f, ctx, history, wf, job)
        if m is None:
            out.append(f)
            continue
        f.kind = Kind.MEASURED
        f.observed_minutes = round(m.observed_minutes, 1)
        f.observed_cost = round(m.observed_cost, 4)
        f.observed_runs = m.observed_runs
        f.recoverable_minutes_est = (
            None if m.recoverable_minutes is None else round(m.recoverable_minutes, 1)
        )
        f.recoverable_cost_est = (
            None if m.recoverable_cost is None else round(m.recoverable_cost, 4)
        )
        f.estimate_method = m.method
        f.confidence = m.confidence
        f.window_days = history.window_days
        f.evidence = list(f.evidence) + m.evidence
        if m.note:
            f.message = f"{f.message} {m.note}"
        if history.jobs_sampled and "sampled" not in (f.estimate_method or ""):
            f.estimate_method = f"{f.estimate_method} (job data available for {_sampled_share(history)} of runs in the window)"
        out.append(f)
    return out


def rounding_findings(existing: list[Finding], ctx: Context, h: HistoryView) -> list[Finding]:
    """History-driven W005: matrix jobs whose measured per-job rounding is a large
    share of billed minutes, even when the matrix is too small for the static rule."""
    from ciburn.findings import Remediation, Severity
    from ciburn.rules.static_matrix import DOCS_URL_ROUNDING, W005

    flagged = {(f.workflow, f.job) for f in existing if f.rule_id == "W005"}
    out: list[Finding] = []
    for wf in ctx.parsed_workflows:
        for job in wf.jobs.values():
            if job.matrix is None or (wf.path, job.key) in flagged:
                continue
            jobs = _jobs_with_data(h, wf, job)
            if len(jobs) < 4:
                continue
            billed = sum(j.billed_minutes for j in jobs)
            waste = sum(j.price.rounding_waste_seconds for j in jobs) / 60.0
            if billed <= 0 or waste / billed < 0.25:
                continue
            rule = W005()
            out.append(
                Finding(
                    rule_id=rule.id,
                    title=rule.title,
                    severity=Severity.LOW,
                    confidence=Confidence.MEDIUM,
                    kind=Kind.ADVISORY,
                    workflow=wf.path,
                    job=job.key,
                    message=f"Job `{job.key}` in {wf.path} runs {job.matrix_legs or 'several'} short matrix legs; "
                    f"per-job round-up to whole minutes is {waste / billed:.0%} of what it bills.",
                    remediation=Remediation(
                        summary="Merge short legs into one job or prune the matrix.",
                        patch={"op": "advise", "job": job.key},
                        docs_url=DOCS_URL_ROUNDING,
                    ),
                    evidence=[
                        Evidence(
                            kind="config",
                            ref=f"{wf.path}#jobs.{job.key}",
                            detail={"legs": job.matrix_legs},
                        )
                    ],
                )
            )
    return out


def _sampled_share(h: HistoryView) -> str:
    with_jobs = sum(1 for r in h.runs if r.jobs_fetched)
    return f"{with_jobs}/{len(h.runs)}"


# --- helpers -----------------------------------------------------------------------


def _cost(jobs: Iterable[JobRecord]) -> float:
    return float(sum((j.cost for j in jobs), Decimal(0)))


def _minutes(jobs: Iterable[JobRecord]) -> float:
    return float(sum(j.billed_minutes for j in jobs))


def _rate(jobs: list[JobRecord]) -> float:
    """Average list price per billed minute across jobs (0 if unpriced)."""
    mins = _minutes(jobs)
    return _cost(jobs) / mins if mins else 0.0


def _job_evidence(jobs: list[JobRecord], limit: int = 5) -> list[Evidence]:
    ev: list[Evidence] = []
    for j in jobs[:limit]:
        ev.append(
            Evidence(
                kind="job",
                ref=f"job:{j.id}",
                detail={
                    "run_id": j.run_id,
                    "name": j.name,
                    "conclusion": j.conclusion,
                    "raw_seconds": round(j.raw_seconds),
                    "billed_minutes": j.billed_minutes,
                    "labels": j.labels,
                    "started_at": j.started_at.isoformat() if j.started_at else None,
                },
            )
        )
    if len(jobs) > limit:
        ev.append(Evidence(kind="job", ref="…", detail={"more_jobs": len(jobs) - limit}))
    return ev


def _run_evidence(runs: list[RunRecord], limit: int = 5) -> list[Evidence]:
    ev = [
        Evidence(
            kind="run",
            ref=f"run:{r.id}",
            detail={
                "event": r.event,
                "conclusion": r.conclusion,
                "head_sha": (r.head_sha or "")[:7],
                "branch": r.head_branch,
                "billed_minutes": r.billed_minutes,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            },
        )
        for r in runs[:limit]
    ]
    if len(runs) > limit:
        ev.append(Evidence(kind="run", ref="…", detail={"more_runs": len(runs) - limit}))
    return ev


def _jobs_with_data(history: HistoryView, wf: Workflow, job: Job | None) -> list[JobRecord]:
    jobs = history.jobs_for(wf.path, job)
    return [j for j in jobs if j.status == "completed" and j.raw_seconds > 0]


def _runs_with_jobs(history: HistoryView, wf: Workflow) -> list[RunRecord]:
    return [r for r in history.runs_for(wf.path) if r.jobs_fetched and r.jobs]


# --- measurers -----------------------------------------------------------------------


def measure_w001(
    f: Finding, ctx: Context, h: HistoryView, wf: Workflow, job: Job | None
) -> Measure | None:
    runs = [r for r in _runs_with_jobs(h, wf) if r.event in PR_EVENTS]
    if not runs:
        return None
    by_branch: dict[str, list[RunRecord]] = {}
    for r in runs:
        by_branch.setdefault(r.head_branch or "", []).append(r)
    superseded: list[RunRecord] = []
    for branch_runs in by_branch.values():
        branch_runs.sort(key=lambda r: (r.created_at or h.since, r.id))
        for a, b in itertools.pairwise(branch_runs):
            if a.conclusion == "cancelled" or not a.updated_at or not b.created_at:
                continue
            if b.created_at < a.updated_at:
                superseded.append(a)
    rec_jobs = [j for r in superseded for j in r.jobs]
    all_jobs = [j for r in runs for j in r.jobs]
    return Measure(
        observed_minutes=_minutes(all_jobs),
        observed_cost=_cost(all_jobs),
        observed_runs=len(runs),
        recoverable_minutes=_minutes(rec_jobs),
        recoverable_cost=_cost(rec_jobs),
        method=f"billed minutes of {len(superseded)} PR runs that a newer run on the same branch "
        "superseded before they finished (upper bound: cancellation would have saved the remainder, not the whole run)",
        confidence=Confidence(confidence_from_count(len(runs))),
        evidence=_run_evidence(superseded),
        note=f"In the last {h.window_days} days, {len(superseded)} of {len(runs)} PR runs were superseded while still running.",
    )


def measure_w002(
    f: Finding, ctx: Context, h: HistoryView, wf: Workflow, job: Job | None
) -> Measure | None:
    jobs = _jobs_with_data(h, wf, job)
    if not jobs:
        return None
    install_seconds = 0.0
    matched: set[str] = set()
    jobs_with_install = 0
    for j in jobs:
        s = 0.0
        for st in j.steps:
            if INSTALL_RE.search(st.name or "") or re.search(r"\binstall\b", st.name or "", re.I):
                s += st.seconds
                matched.add(st.name)
        if s > 0:
            jobs_with_install += 1
        install_seconds += s
    total_seconds = sum(j.raw_seconds for j in jobs)
    share = install_seconds / total_seconds if total_seconds else 0.0
    if jobs_with_install == 0:
        return Measure(
            observed_minutes=_minutes(jobs),
            observed_cost=_cost(jobs),
            observed_runs=len(jobs),
            recoverable_minutes=None,
            recoverable_cost=None,
            method="dependency-install step time could not be isolated from step names; no estimate",
            confidence=Confidence.LOW,
            evidence=_job_evidence(jobs),
        )
    rec_min = install_seconds / 60.0 * 0.5
    return Measure(
        observed_minutes=_minutes(jobs),
        observed_cost=_cost(jobs),
        observed_runs=len(jobs),
        recoverable_minutes=rec_min,
        recoverable_cost=rec_min * _rate(jobs),
        method=f"half of the measured dependency-install step time ({install_seconds / 60:.0f} min across "
        f"{jobs_with_install} jobs, steps: {', '.join(sorted(matched)[:3])}); a warm cache typically removes most of it",
        confidence=Confidence(confidence_from_count(jobs_with_install)),
        evidence=_job_evidence(jobs),
        note=f"Dependency installation is {share:.0%} of this job's measured time.",
    )


def measure_w003(
    f: Finding, ctx: Context, h: HistoryView, wf: Workflow, job: Job | None
) -> Measure | None:
    jobs = _jobs_with_data(h, wf, job)
    if not jobs:
        return None
    mins = [j.raw_seconds / 60.0 for j in jobs]
    p95 = percentile(mins, 95)
    recommended = max(5, math.ceil(p95 * 1.5))
    over = [j for j in jobs if j.billed_minutes > recommended]
    rec_min = sum(j.billed_minutes - recommended for j in over)
    f.remediation.patch["value"] = recommended
    f.remediation.summary = (
        f"Set `timeout-minutes: {recommended}` (1.5x the observed p95 of {p95:.1f} min)."
    )
    return Measure(
        observed_minutes=_minutes(jobs),
        observed_cost=_cost(jobs),
        observed_runs=len(jobs),
        recoverable_minutes=float(rec_min),
        recoverable_cost=rec_min * _rate(jobs),
        method=f"minutes billed beyond a {recommended}-minute timeout (1.5x observed p95); {len(over)} of {len(jobs)} jobs exceeded it",
        confidence=Confidence(confidence_from_count(len(jobs))),
        evidence=_job_evidence(sorted(over, key=lambda j: -j.raw_seconds)),
        note=f"Observed p50 {median(mins):.1f} min, p95 {p95:.1f} min, max {max(mins):.1f} min.",
    )


def measure_w004(
    f: Finding, ctx: Context, h: HistoryView, wf: Workflow, job: Job | None
) -> Measure | None:
    trees = [str(t) for t in f.remediation.patch.get("value", [])]
    runs = [r for r in _runs_with_jobs(h, wf) if r.event in ("push", *PR_EVENTS)]
    if not runs or not h.commit_files:
        return None
    known = [r for r in runs if r.head_sha and h.commit_files.get(r.head_sha)]
    if not known:
        return None
    docs_only = [
        r for r in known if all(_matches_tree(p, trees) for p in h.commit_files[r.head_sha or ""])
    ]
    rec_jobs = [j for r in docs_only for j in r.jobs]
    return Measure(
        observed_minutes=_minutes([j for r in runs for j in r.jobs]),
        observed_cost=_cost([j for r in runs for j in r.jobs]),
        observed_runs=len(runs),
        recoverable_minutes=_minutes(rec_jobs),
        recoverable_cost=_cost(rec_jobs),
        method=f"billed minutes of {len(docs_only)} runs whose head commit touched only {', '.join(trees)} "
        f"({len(known)} of {len(runs)} runs have commit file lists)",
        confidence=Confidence(confidence_from_count(len(known))),
        evidence=_run_evidence(docs_only),
    )


def _matches_tree(path: str, trees: list[str]) -> bool:
    for t in trees:
        if t.startswith("**."):
            if path.endswith(t[2:]):
                return True
        elif t.endswith("/**"):
            if path.startswith(t[:-2]):
                return True
        elif path == t:
            return True
    return False


def measure_w005(
    f: Finding, ctx: Context, h: HistoryView, wf: Workflow, job: Job | None
) -> Measure | None:
    jobs = _jobs_with_data(h, wf, job)
    if not jobs:
        return None
    by_run: dict[tuple[int, int], list[JobRecord]] = {}
    for j in jobs:
        by_run.setdefault((j.run_id, j.run_attempt), []).append(j)
    billed = 0
    merged = 0
    waste_seconds = 0.0
    for legs in by_run.values():
        billed += sum(j.billed_minutes for j in legs)
        merged += billable_minutes(sum(j.raw_seconds for j in legs))
        waste_seconds += sum(j.price.rounding_waste_seconds for j in legs)
    rec = float(billed - merged)
    avg_leg = sum(j.raw_seconds for j in jobs) / len(jobs) / 60.0
    return Measure(
        observed_minutes=float(billed),
        observed_cost=_cost(jobs),
        observed_runs=len(by_run),
        recoverable_minutes=rec,
        recoverable_cost=rec * _rate(jobs),
        method="billed minutes minus the minutes the same work would bill if every leg of a run shared one job "
        "(per-job round-up removed; assumes legs run sequentially on one runner)",
        confidence=Confidence(confidence_from_count(len(by_run))),
        evidence=_job_evidence(jobs),
        note=f"Average leg {avg_leg:.1f} min; per-job rounding added {waste_seconds / 60:.0f} min "
        f"({waste_seconds / 60 / billed:.0%} of billed) across {len(jobs)} legs in {len(by_run)} runs.",
    )


def measure_w006(
    f: Finding, ctx: Context, h: HistoryView, wf: Workflow, job: Job | None
) -> Measure | None:
    jobs = _jobs_with_data(h, wf, job)
    if not jobs:
        return None
    deep = [j.step_seconds_matching(CHECKOUT_STEP_RE) for j in jobs]
    deep = [d for d in deep if d > 0]
    if not deep:
        return None
    shallow: list[float] = []
    for other in h.completed_jobs():
        cj = other.config_job
        if cj is None or cj is job or _uses_full_history(cj):
            continue
        s = other.step_seconds_matching(CHECKOUT_STEP_RE)
        if s > 0:
            shallow.append(s)
    baseline = median(shallow) if shallow else 10.0
    src = (
        f"median shallow checkout of {len(shallow)} other jobs in this repository"
        if shallow
        else "assumed 10 s"
    )
    rec_sec = sum(max(0.0, d - baseline) for d in deep)
    return Measure(
        observed_minutes=_minutes(jobs),
        observed_cost=_cost(jobs),
        observed_runs=len(jobs),
        recoverable_minutes=rec_sec / 60.0,
        recoverable_cost=rec_sec / 60.0 * _rate(jobs),
        method=f"checkout step time above a {baseline:.0f} s baseline ({src}); only the step time, "
        "not rounding effects",
        confidence=Confidence.MEDIUM if shallow and len(deep) >= 10 else Confidence.LOW,
        evidence=_job_evidence(jobs),
        note=f"Full-history checkout took a median {median(deep):.0f} s per job here.",
    )


def _uses_full_history(job: Job) -> bool:
    return any(
        s.action_repo == "actions/checkout" and str(s.with_.get("fetch-depth", "")).strip() == "0"
        for s in job.steps
    )


def measure_w007(
    f: Finding, ctx: Context, h: HistoryView, wf: Workflow, job: Job | None
) -> Measure | None:
    jobs = _jobs_with_data(h, wf, job)
    if not jobs:
        return None
    mins = [j.raw_seconds / 60.0 for j in jobs]
    p95 = percentile(mins, 95)
    limit = ctx.pricing.ubuntu_slim_job_timeout_minutes
    slim = float(ctx.model.sku("actions_linux_slim").per_minute)
    std = float(ctx.model.sku("actions_linux").per_minute)
    cost = _cost(jobs)
    if p95 > limit * 0.8:
        return Measure(
            observed_minutes=_minutes(jobs),
            observed_cost=cost,
            observed_runs=len(jobs),
            recoverable_minutes=None,
            recoverable_cost=None,
            method=f"no estimate: observed p95 {p95:.1f} min is too close to the {limit}-minute ubuntu-slim limit",
            confidence=Confidence.LOW,
            evidence=_job_evidence(jobs),
        )
    rec_cost = cost * (1 - slim / std) if std else 0.0
    return Measure(
        observed_minutes=_minutes(jobs),
        observed_cost=cost,
        observed_runs=len(jobs),
        recoverable_minutes=None,
        recoverable_cost=rec_cost,
        method=f"same billed minutes at the ubuntu-slim rate (${slim}/min vs ${std}/min); assumes equal "
        "wall time on 1 vCPU, which holds for I/O-bound lint/script jobs, not CPU-bound ones",
        confidence=Confidence.MEDIUM if len(jobs) >= 10 else Confidence.LOW,
        evidence=_job_evidence(jobs),
        note=f"Observed p50 {median(mins):.1f} min, p95 {p95:.1f} min over {len(jobs)} jobs.",
    )


def measure_w008(
    f: Finding, ctx: Context, h: HistoryView, wf: Workflow, job: Job | None
) -> Measure | None:
    runs = [r for r in _runs_with_jobs(h, wf) if r.event == "schedule"]
    if len(runs) < 2:
        return None
    runs.sort(key=lambda r: (r.created_at or h.since, r.id))
    repeated = [b for a, b in itertools.pairwise(runs) if a.head_sha and a.head_sha == b.head_sha]
    rec_jobs = [j for r in repeated for j in r.jobs]
    all_jobs = [j for r in runs for j in r.jobs]
    return Measure(
        observed_minutes=_minutes(all_jobs),
        observed_cost=_cost(all_jobs),
        observed_runs=len(runs),
        recoverable_minutes=_minutes(rec_jobs),
        recoverable_cost=_cost(rec_jobs),
        method=f"billed minutes of {len(repeated)} scheduled runs whose commit was identical to the previous "
        f"scheduled run's ({len(runs)} scheduled runs with job data)",
        confidence=Confidence(confidence_from_count(len(runs))),
        evidence=_run_evidence(repeated),
    )


def measure_w009(
    f: Finding, ctx: Context, h: HistoryView, wf: Workflow, job: Job | None
) -> Measure | None:
    runs = _runs_with_jobs(h, wf)
    by_sha: dict[str, list[RunRecord]] = {}
    for r in runs:
        if r.head_sha and r.event in ("push", *PR_EVENTS):
            by_sha.setdefault(r.head_sha, []).append(r)
    dup_push: list[RunRecord] = []
    for group in by_sha.values():
        events = {r.event for r in group}
        if "push" in events and events & set(PR_EVENTS):
            dup_push.extend(r for r in group if r.event == "push")
    if not by_sha:
        return None
    rec_jobs = [j for r in dup_push for j in r.jobs]
    all_jobs = [j for r in runs for j in r.jobs]
    return Measure(
        observed_minutes=_minutes(all_jobs),
        observed_cost=_cost(all_jobs),
        observed_runs=len(runs),
        recoverable_minutes=_minutes(rec_jobs),
        recoverable_cost=_cost(rec_jobs),
        method=f"billed minutes of {len(dup_push)} push-event runs whose commit also had a pull_request run",
        confidence=Confidence(confidence_from_count(len(runs))),
        evidence=_run_evidence(dup_push),
    )


def measure_w010(
    f: Finding, ctx: Context, h: HistoryView, wf: Workflow, job: Job | None
) -> Measure | None:
    jobs = [j for j in _jobs_with_data(h, wf, job) if j.event in PR_EVENTS]
    if not jobs:
        return None
    return Measure(
        observed_minutes=_minutes(jobs),
        observed_cost=_cost(jobs),
        observed_runs=len(jobs),
        recoverable_minutes=None,
        recoverable_cost=None,
        method="no estimate: draft status of past pull requests is not in the run history",
        confidence=Confidence(confidence_from_count(len(jobs))),
        evidence=_job_evidence(jobs),
    )


def measure_w012(
    f: Finding, ctx: Context, h: HistoryView, wf: Workflow, job: Job | None
) -> Measure | None:
    jobs = _jobs_with_data(h, wf, job)
    if not jobs:
        return None
    by_run: dict[tuple[int, int], list[JobRecord]] = {}
    for j in jobs:
        by_run.setdefault((j.run_id, j.run_attempt), []).append(j)
    rec_min = 0.0
    affected = 0
    for legs in by_run.values():
        failed = [j for j in legs if j.failed and j.completed_at]
        if not failed:
            continue
        first_fail = min(j.completed_at for j in failed if j.completed_at is not None)
        for j in legs:
            if j.failed or not j.completed_at or j.completed_at <= first_fail:
                continue
            start = j.started_at or first_fail
            after = (j.completed_at - max(start, first_fail)).total_seconds()
            rec_min += min(j.billed_minutes, after / 60.0)
        affected += 1
    return Measure(
        observed_minutes=_minutes(jobs),
        observed_cost=_cost(jobs),
        observed_runs=len(by_run),
        recoverable_minutes=rec_min,
        recoverable_cost=rec_min * _rate(jobs),
        method=f"minutes sibling legs kept running after the first leg failed, in {affected} of {len(by_run)} runs",
        confidence=Confidence(confidence_from_count(affected)),
        evidence=_job_evidence(jobs),
    )


MEASURERS: dict[str, Measurer] = {
    "W001": measure_w001,
    "W002": measure_w002,
    "W003": measure_w003,
    "W004": measure_w004,
    "W005": measure_w005,
    "W006": measure_w006,
    "W007": measure_w007,
    "W008": measure_w008,
    "W009": measure_w009,
    "W010": measure_w010,
    "W012": measure_w012,
}
