"""History rules H001-H009. Every finding here is measured by construction."""

from __future__ import annotations

import itertools
import re
from collections.abc import Sequence
from decimal import Decimal

from ciburn.findings import Confidence, Evidence, Finding, Kind, Remediation, Severity
from ciburn.history.view import HistoryView, JobRecord, RunRecord
from ciburn.rules.base import Context, Rule
from ciburn.stats import confidence_from_count, median, percentile

DOCS_SCHEDULE = (
    "https://docs.github.com/en/actions/writing-workflows/choosing-when-your-workflow-runs/"
    "events-that-trigger-workflows#schedule"
)
DOCS_RERUN = "https://docs.github.com/en/actions/managing-workflow-runs-and-deployments/managing-workflow-runs/re-running-workflows-and-jobs"
DOCS_TIMEOUT = "https://docs.github.com/en/actions/writing-workflows/workflow-syntax-for-github-actions#jobsjob_idtimeout-minutes"
DOCS_NEEDS = "https://docs.github.com/en/actions/writing-workflows/workflow-syntax-for-github-actions#jobsjob_idneeds"
CHEAP_GATE_RE = re.compile(
    r"(lint|format|fmt|check|typecheck|mypy|ruff|eslint|prettier|clippy|pre-commit|static)", re.I
)


def _cost(jobs: Sequence[JobRecord]) -> float:
    return float(sum((j.cost for j in jobs), Decimal(0)))


def _minutes(jobs: Sequence[JobRecord]) -> float:
    return float(sum(j.billed_minutes for j in jobs))


def _rate(jobs: Sequence[JobRecord]) -> float:
    m = _minutes(jobs)
    return _cost(jobs) / m if m else 0.0


def _run_ev(runs: Sequence[RunRecord], limit: int = 5) -> list[Evidence]:
    ev = [
        Evidence(
            kind="run",
            ref=f"run:{r.id}",
            detail={
                "event": r.event,
                "conclusion": r.conclusion,
                "head_sha": (r.head_sha or "")[:7],
                "billed_minutes": r.billed_minutes,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            },
        )
        for r in runs[:limit]
    ]
    if len(runs) > limit:
        ev.append(Evidence(kind="run", ref="…", detail={"more_runs": len(runs) - limit}))
    return ev


def _job_ev(jobs: Sequence[JobRecord], limit: int = 5) -> list[Evidence]:
    ev = [
        Evidence(
            kind="job",
            ref=f"job:{j.id}",
            detail={
                "run_id": j.run_id,
                "name": j.name,
                "conclusion": j.conclusion,
                "raw_seconds": round(j.raw_seconds),
                "billed_minutes": j.billed_minutes,
                "run_attempt": j.run_attempt,
            },
        )
        for j in jobs[:limit]
    ]
    if len(jobs) > limit:
        ev.append(Evidence(kind="job", ref="…", detail={"more_jobs": len(jobs) - limit}))
    return ev


def _measured(
    rule: Rule,
    *,
    workflow: str | None,
    job: str | None,
    message: str,
    remediation: Remediation,
    observed: Sequence[JobRecord],
    runs: int,
    recoverable_minutes: float | None,
    recoverable_cost: float | None,
    method: str,
    confidence: Confidence,
    evidence: list[Evidence],
    severity: Severity,
    window_days: int,
    tags: Sequence[str] = (),
) -> Finding:
    return Finding(
        rule_id=str(rule.id),
        title=str(rule.title),
        severity=severity,
        confidence=confidence,
        kind=Kind.MEASURED,
        message=message,
        remediation=remediation,
        workflow=workflow,
        job=job,
        observed_minutes=round(_minutes(observed), 1),
        observed_cost=round(_cost(observed), 4),
        observed_runs=runs,
        recoverable_minutes_est=None
        if recoverable_minutes is None
        else round(recoverable_minutes, 1),
        recoverable_cost_est=None if recoverable_cost is None else round(recoverable_cost, 4),
        estimate_method=method,
        window_days=window_days,
        evidence=evidence,
        tags=list(tags),
    )


def _scheduled_runs(h: HistoryView, path: str) -> list[RunRecord]:
    runs = [r for r in h.runs_for(path) if r.event == "schedule" and r.status == "completed"]
    runs.sort(key=lambda r: (r.created_at or h.since, r.id))
    return runs


class H001:
    id = "H001"
    title = "Scheduled workflow failing consecutively (dead cron)"
    severity = Severity.HIGH
    needs_history = True
    k = 3

    def run(self, ctx: Context) -> list[Finding]:
        h = ctx.history
        assert h is not None
        out: list[Finding] = []
        for path in sorted({r.path for r in h.runs if r.event == "schedule"}):
            runs = _scheduled_runs(h, path)
            streaks: list[list[RunRecord]] = []
            cur: list[RunRecord] = []
            for r in runs:
                if r.failed:
                    cur.append(r)
                else:
                    if len(cur) >= self.k:
                        streaks.append(cur)
                    cur = []
            tail_failing = len(cur) >= self.k
            if tail_failing:
                streaks.append(cur)
            if not streaks:
                continue
            wasted = [r for s in streaks for r in s[self.k :]]
            failing = [r for s in streaks for r in s]
            wasted_jobs = [j for r in wasted for j in r.jobs]
            failing_jobs = [j for r in failing for j in r.jobs]
            longest = max(len(s) for s in streaks)
            has_jobs = any(r.jobs_fetched for r in failing)
            msg = (
                f"{path} is scheduled and failed {longest} times in a row"
                + (" and is still failing" if tail_failing else "")
                + f"; {len(wasted)} runs after the {self.k}rd consecutive failure produced nothing but minutes."
            )
            out.append(
                _measured(
                    self,
                    workflow=path,
                    job=None,
                    message=msg,
                    remediation=Remediation(
                        summary="Fix or disable the schedule; add a step that disables the workflow after k failures.",
                        patch={"op": "advise", "workflow": path},
                        docs_url=DOCS_SCHEDULE,
                    ),
                    observed=failing_jobs,
                    runs=len(failing),
                    recoverable_minutes=_minutes(wasted_jobs),
                    recoverable_cost=_cost(wasted_jobs),
                    method=f"billed minutes of scheduled runs after the {self.k}rd consecutive failure"
                    + (
                        "" if has_jobs else " (no job data fetched for these runs; minutes unknown)"
                    ),
                    confidence=Confidence.HIGH if has_jobs else Confidence.MEDIUM,
                    evidence=_run_ev(wasted or failing),
                    severity=Severity.HIGH if tail_failing else Severity.MEDIUM,
                    window_days=h.window_days,
                )
            )
        return out


class H002:
    id = "H002"
    title = "Scheduled runs on a commit already tested by the previous scheduled run"
    severity = Severity.MEDIUM
    needs_history = True

    def run(self, ctx: Context) -> list[Finding]:
        h = ctx.history
        assert h is not None
        out: list[Finding] = []
        for path in sorted({r.path for r in h.runs if r.event == "schedule"}):
            runs = _scheduled_runs(h, path)
            if len(runs) < 3:
                continue
            repeated = [
                b for a, b in itertools.pairwise(runs) if a.head_sha and a.head_sha == b.head_sha
            ]
            if not repeated:
                continue
            rep_jobs = [j for r in repeated for j in r.jobs]
            all_jobs = [j for r in runs for j in r.jobs]
            share = len(repeated) / len(runs)
            out.append(
                _measured(
                    self,
                    workflow=path,
                    job=None,
                    message=f"{path}: {len(repeated)} of {len(runs)} scheduled runs ({share:.0%}) ran on exactly the "
                    "commit the previous scheduled run had already tested.",
                    remediation=Remediation(
                        summary="Skip the run when the head commit has not changed since the last scheduled run "
                        "(compare `git rev-parse HEAD` to a cached value), or lower the frequency.",
                        patch={"op": "advise", "workflow": path},
                        docs_url=DOCS_SCHEDULE,
                    ),
                    observed=all_jobs,
                    runs=len(runs),
                    recoverable_minutes=_minutes(rep_jobs),
                    recoverable_cost=_cost(rep_jobs),
                    method="billed minutes of scheduled runs whose head_sha equals the previous scheduled run's",
                    confidence=Confidence(confidence_from_count(len(runs))),
                    evidence=_run_ev(repeated),
                    severity=Severity.MEDIUM if share >= 0.2 else Severity.LOW,
                    window_days=h.window_days,
                )
            )
        return out


class H003:
    id = "H003"
    title = "Job that has never failed: candidate for sampling or removal"
    severity = Severity.LOW
    needs_history = True
    min_runs = 30

    def run(self, ctx: Context) -> list[Finding]:
        h = ctx.history
        assert h is not None
        out: list[Finding] = []
        for (path, base), jobs in sorted(h.job_groups().items()):
            done = [
                j
                for j in jobs
                if j.status == "completed" and j.conclusion in ("success", "failure", "timed_out")
            ]
            if len(done) < self.min_runs or any(j.failed for j in done):
                continue
            # only jobs that gate code changes; scheduled/manual maintenance jobs are not "gates"
            if (
                sum(1 for j in done if j.event in ("push", "pull_request", "pull_request_target"))
                < len(done) / 2
            ):
                continue
            if any(
                j.event in ("release", "workflow_dispatch", "push")
                and re.search(r"(deploy|publish|release)", base, re.I)
                for j in done
            ):
                continue
            mins = _minutes(done)
            if mins < 30:
                continue
            out.append(
                _measured(
                    self,
                    workflow=path,
                    job=base,
                    message=f"`{base}` succeeded in all {len(done)} completed runs in the window and consumed "
                    f"{mins:.0f} billed minutes; it has caught nothing in {h.window_days} days.",
                    remediation=Remediation(
                        summary="Run it less often (main branch only, nightly, or a subset of the matrix) and watch "
                        "whether it ever fails.",
                        patch={"op": "advise", "job": base},
                        docs_url=DOCS_NEEDS,
                    ),
                    observed=done,
                    runs=len(done),
                    recoverable_minutes=mins / 2,
                    recoverable_cost=_cost(done) / 2,
                    method="scenario: running the job on half of the events (e.g. main only) halves its minutes; "
                    "whether it would have caught a regression is unknowable from history",
                    confidence=Confidence.LOW,
                    evidence=_job_ev(done),
                    severity=Severity.LOW,
                    window_days=h.window_days,
                )
            )
        return out


class H004:
    id = "H004"
    title = "Failure hotspot: failed runs of this job consume a disproportionate share of minutes"
    severity = Severity.MEDIUM
    needs_history = True

    def run(self, ctx: Context) -> list[Finding]:
        h = ctx.history
        assert h is not None
        out: list[Finding] = []
        for (path, base), jobs in sorted(h.job_groups().items()):
            done = [j for j in jobs if j.status == "completed" and j.raw_seconds > 0]
            failed = [j for j in done if j.failed]
            if len(done) < 10 or len(failed) < 3:
                continue
            fmin, tmin = _minutes(failed), _minutes(done)
            if tmin <= 0:
                continue
            min_share = fmin / tmin
            cnt_share = len(failed) / len(done)
            if min_share < 0.25 or min_share < 1.5 * cnt_share:
                continue
            ok = [j.raw_seconds / 60 for j in done if not j.failed]
            p50_ok = median(ok) if ok else 0.0
            late = sum(max(0.0, j.raw_seconds / 60 - p50_ok) for j in failed)
            out.append(
                _measured(
                    self,
                    workflow=path,
                    job=base,
                    message=f"`{base}`: {len(failed)} of {len(done)} jobs failed ({cnt_share:.0%}) but failures took "
                    f"{min_share:.0%} of its billed minutes; failing jobs ran a median "
                    f"{median([j.raw_seconds / 60 for j in failed]):.1f} min vs {p50_ok:.1f} min for passing ones.",
                    remediation=Remediation(
                        summary="Move the failing check earlier in the job (fail fast) or split it into its own gate.",
                        patch={"op": "advise", "job": base},
                        docs_url=DOCS_NEEDS,
                    ),
                    observed=failed,
                    runs=len(done),
                    recoverable_minutes=late,
                    recoverable_cost=late * _rate(done),
                    method="minutes failing jobs ran beyond the median duration of passing jobs (what failing earlier "
                    "would have saved)",
                    confidence=Confidence(confidence_from_count(len(failed))),
                    evidence=_job_ev(sorted(failed, key=lambda j: -j.raw_seconds)),
                    severity=Severity.MEDIUM,
                    window_days=h.window_days,
                )
            )
        return out


class H005:
    id = "H005"
    title = "Runtime tail: p95 far above the median (flaky, hanging or timing out)"
    severity = Severity.MEDIUM
    needs_history = True

    def run(self, ctx: Context) -> list[Finding]:
        h = ctx.history
        assert h is not None
        out: list[Finding] = []
        for (path, base), jobs in sorted(h.job_groups().items()):
            done = [j for j in jobs if j.status == "completed" and j.raw_seconds > 0]
            if len(done) < 20:
                continue
            mins = [j.raw_seconds / 60 for j in done]
            p50, p95 = median(mins), percentile(mins, 95)
            if p50 < 1 or p95 < 3 * p50:
                continue
            tail = [j for j in done if j.raw_seconds / 60 > 2 * p50]
            rec = sum(j.raw_seconds / 60 - 2 * p50 for j in tail)
            out.append(
                _measured(
                    self,
                    workflow=path,
                    job=base,
                    message=f"`{base}` runs {p50:.1f} min at the median but {p95:.1f} min at p95 ({p95 / p50:.1f}x); "
                    f"{len(tail)} of {len(done)} jobs took more than twice the median.",
                    remediation=Remediation(
                        summary="Find the slow tail (hung network calls, retries, flaky tests) and set a timeout near 2x median.",
                        patch={
                            "op": "add_job_key",
                            "job": base,
                            "key": "timeout-minutes",
                            "value": max(5, round(2 * p50) + 1),
                        },
                        docs_url=DOCS_TIMEOUT,
                    ),
                    observed=done,
                    runs=len(done),
                    recoverable_minutes=rec,
                    recoverable_cost=rec * _rate(done),
                    method="minutes above 2x the median across jobs slower than that",
                    confidence=Confidence(confidence_from_count(len(tail), high=15, medium=5)),
                    evidence=_job_ev(sorted(tail, key=lambda j: -j.raw_seconds)),
                    severity=Severity.MEDIUM,
                    window_days=h.window_days,
                )
            )
        return out


class H006:
    id = "H006"
    title = "Re-run tax: minutes spent re-running the same commit"
    severity = Severity.MEDIUM
    needs_history = True

    def run(self, ctx: Context) -> list[Finding]:
        h = ctx.history
        assert h is not None
        out: list[Finding] = []
        by_wf: dict[str, list[JobRecord]] = {}
        for j in h.jobs:
            by_wf.setdefault(j.workflow_path, []).append(j)
        for path, jobs in sorted(by_wf.items()):
            reruns = [j for j in jobs if j.run_attempt >= 2 and j.status == "completed"]
            if not reruns:
                continue
            total = _minutes(jobs)
            rmin = _minutes(reruns)
            if rmin < 5:
                continue
            share = rmin / total if total else 0.0
            runs = len({j.run_id for j in reruns})
            out.append(
                _measured(
                    self,
                    workflow=path,
                    job=None,
                    message=f"{path}: {runs} runs were re-run; attempts 2+ consumed {rmin:.0f} billed minutes "
                    f"({share:.0%} of this workflow's minutes).",
                    remediation=Remediation(
                        summary="Use `re-run failed jobs` instead of the whole run, and fix the flaky tests that cause re-runs.",
                        patch={"op": "advise", "workflow": path},
                        docs_url=DOCS_RERUN,
                    ),
                    observed=reruns,
                    runs=runs,
                    recoverable_minutes=rmin,
                    recoverable_cost=_cost(reruns),
                    method="billed minutes of jobs in run attempts >= 2 (upper bound: some re-runs are legitimate)",
                    confidence=Confidence.HIGH,
                    evidence=_job_ev(reruns),
                    severity=Severity.MEDIUM if share >= 0.1 else Severity.LOW,
                    window_days=h.window_days,
                )
            )
        return out


class H007:
    id = "H007"
    title = "Jobs killed by the default 360-minute timeout"
    severity = Severity.HIGH
    needs_history = True

    def run(self, ctx: Context) -> list[Finding]:
        h = ctx.history
        assert h is not None
        default = ctx.pricing.default_job_timeout_minutes
        out: list[Finding] = []
        for (path, base), jobs in sorted(h.job_groups().items()):
            hits = [
                j
                for j in jobs
                if j.conclusion == "timed_out"
                and j.raw_seconds >= default * 60 * 0.97
                and (j.config_job is None or j.config_job.timeout_minutes is None)
            ]
            if not hits:
                continue
            ok = [
                j.raw_seconds / 60
                for j in jobs
                if j.status == "completed" and not j.failed and j.raw_seconds > 0
            ]
            p95 = percentile(ok, 95) if ok else 0.0
            recommended = max(5, round(p95 * 1.5)) if ok else 60
            rec = sum(max(0.0, j.billed_minutes - recommended) for j in hits)
            out.append(
                _measured(
                    self,
                    workflow=path,
                    job=base,
                    message=f"`{base}` hit GitHub's {default}-minute default timeout {len(hits)} time(s); "
                    + (
                        f"successful runs finish in {p95:.0f} min at p95."
                        if ok
                        else "no successful run was observed."
                    ),
                    remediation=Remediation(
                        summary=f"Set `timeout-minutes: {recommended}`.",
                        patch={
                            "op": "add_job_key",
                            "job": base,
                            "key": "timeout-minutes",
                            "value": recommended,
                        },
                        docs_url=DOCS_TIMEOUT,
                    ),
                    observed=hits,
                    runs=len(hits),
                    recoverable_minutes=rec,
                    recoverable_cost=rec * _rate(hits) if _rate(hits) else rec * _rate(jobs),
                    method=f"minutes billed beyond a {recommended}-minute timeout on the timed-out jobs",
                    confidence=Confidence.HIGH,
                    evidence=_job_ev(hits),
                    severity=Severity.HIGH,
                    window_days=h.window_days,
                )
            )
        return out


class H008:
    id = "H008"
    title = "Queue time is a large share of wall time"
    severity = Severity.LOW
    needs_history = True

    def run(self, ctx: Context) -> list[Finding]:
        h = ctx.history
        assert h is not None
        out: list[Finding] = []
        for (path, base), jobs in sorted(h.job_groups().items()):
            done = [j for j in jobs if j.status == "completed" and j.raw_seconds > 0]
            if len(done) < 10:
                continue
            q = sum(j.queue_seconds for j in done) / 60
            e = sum(j.raw_seconds for j in done) / 60
            if q < 60 or q / (q + e) < 0.2:
                continue
            out.append(
                _measured(
                    self,
                    workflow=path,
                    job=base,
                    message=f"`{base}` waited {q:.0f} min in queue against {e:.0f} min of execution "
                    f"({q / (q + e):.0%} of wall time); queue time is not billed but it is latency.",
                    remediation=Remediation(
                        summary="Check runner concurrency limits for the plan/labels; consolidate short jobs.",
                        patch={"op": "advise", "job": base},
                        docs_url="https://docs.github.com/en/actions/reference/limits",
                    ),
                    observed=done,
                    runs=len(done),
                    recoverable_minutes=None,
                    recoverable_cost=None,
                    method="queue minutes = started_at - created_at per job; not billed, no money estimate",
                    confidence=Confidence.HIGH,
                    evidence=_job_ev(sorted(done, key=lambda j: -j.queue_seconds)),
                    severity=Severity.LOW,
                    window_days=h.window_days,
                    tags=["latency-not-money"],
                )
            )
        return out


class H009:
    id = "H009"
    title = "Expensive job runs in parallel with a cheap gate that fails"
    severity = Severity.MEDIUM
    needs_history = True

    def run(self, ctx: Context) -> list[Finding]:
        h = ctx.history
        assert h is not None
        out: list[Finding] = []
        groups = h.job_groups()
        by_wf: dict[str, list[tuple[str, list[JobRecord]]]] = {}
        for (path, base), jobs in groups.items():
            by_wf.setdefault(path, []).append((base, jobs))
        for path, items in sorted(by_wf.items()):
            cheap = [
                (b, js)
                for b, js in items
                if CHEAP_GATE_RE.search(b)
                and median([j.raw_seconds / 60 for j in js]) <= 3
                and sum(1 for j in js if j.failed) >= 3
            ]
            if not cheap:
                continue
            for base, jobs in items:
                done = [j for j in jobs if j.status == "completed" and j.raw_seconds > 0]
                if len(done) < 10 or median([j.raw_seconds / 60 for j in done]) < 5:
                    continue
                cfg = next((j.config_job for j in done if j.config_job), None)
                for cbase, cjobs in cheap:
                    if cbase == base:
                        continue
                    if cfg is not None and any(
                        n == cbase or cbase.startswith(n) for n in cfg.needs
                    ):
                        continue
                    failed_runs = {(j.run_id, j.run_attempt) for j in cjobs if j.failed}
                    wasted = [j for j in done if (j.run_id, j.run_attempt) in failed_runs]
                    if len(wasted) < 3:
                        continue
                    out.append(
                        _measured(
                            self,
                            workflow=path,
                            job=base,
                            message=f"`{base}` (median {median([j.raw_seconds / 60 for j in done]):.1f} min) ran in "
                            f"{len(wasted)} runs where the cheap gate `{cbase}` failed; it does not depend on it.",
                            remediation=Remediation(
                                summary=f"Add `needs: [{cbase}]` so the expensive job waits for the cheap gate.",
                                patch={
                                    "op": "add_job_key",
                                    "job": base,
                                    "key": "needs",
                                    "value": [cbase],
                                },
                                docs_url=DOCS_NEEDS,
                            ),
                            observed=wasted,
                            runs=len(wasted),
                            recoverable_minutes=_minutes(wasted),
                            recoverable_cost=_cost(wasted),
                            method=f"billed minutes of `{base}` in runs where `{cbase}` failed (the gate's own median "
                            f"is {median([j.raw_seconds / 60 for j in cjobs]):.1f} min of added latency when it passes)",
                            confidence=Confidence(confidence_from_count(len(wasted))),
                            evidence=_job_ev(wasted),
                            severity=Severity.MEDIUM,
                            window_days=h.window_days,
                        )
                    )
        return out


HISTORY_RULES: list[Rule] = [H001(), H002(), H003(), H004(), H005(), H006(), H007(), H008(), H009()]
