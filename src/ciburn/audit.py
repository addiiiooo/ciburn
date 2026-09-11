"""Audit orchestration: static rules + history + join + history rules -> AuditResult."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ciburn import __version__
from ciburn.findings import Finding, Kind, Severity, sort_findings
from ciburn.history.client import AuthError, GitHubClient, GitHubError, NotFoundError
from ciburn.history.ingest import IngestOptions, IngestResult, ingest
from ciburn.history.store import Store, default_cache_path
from ciburn.history.view import HistoryView, Totals
from ciburn.join import join_findings
from ciburn.pricing import (
    DEFAULT_MODEL,
    IncludedMinutesResult,
    PriceModel,
    Pricing,
    apply_included_minutes,
)
from ciburn.repo_layout import RepoLayout
from ciburn.rules import HISTORY_RULES, STATIC_RULES, Context, run_rules
from ciburn.workflow import Workflow, load_workflow_texts, load_workflows

Progress = Callable[[str, dict[str, Any]], None]
REMOTE_RE = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


@dataclass
class AuditOptions:
    path: Path | None = None
    repo: str | None = None
    days: int = 90
    model_id: str = DEFAULT_MODEL
    plan: str | None = None
    cache: Path | None = None
    token: str | None = None
    no_history: bool = False
    offline: bool = False
    label_overrides: dict[str, str] = field(default_factory=dict)
    max_job_runs: int | None = None
    fetch_commits: bool = True
    ignore: set[str] = field(default_factory=set)
    progress: Progress | None = None
    now: datetime | None = None


@dataclass
class AuditResult:
    generated_at: datetime
    pricing: Pricing
    model: PriceModel
    window_days: int
    workflows: list[Workflow]
    findings: list[Finding]
    repo: str | None = None
    path: str | None = None
    history: HistoryView | None = None
    totals: Totals | None = None
    included: IncludedMinutesResult | None = None
    ingest: IngestResult | None = None
    notes: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def measured(self) -> list[Finding]:
        return [f for f in self.findings if f.kind is Kind.MEASURED]

    @property
    def advisory(self) -> list[Finding]:
        return [f for f in self.findings if f.kind is Kind.ADVISORY]

    def worst_severity(self, measured_only: bool = False) -> Severity | None:
        fs = self.measured if measured_only else self.findings
        if not fs:
            return None
        return max((f.severity for f in fs), key=lambda s: s.rank)

    def recoverable_cost_total(self) -> float:
        return sum(f.recoverable_cost_est or 0.0 for f in self.measured)

    def recoverable_minutes_total(self) -> float:
        return sum(f.recoverable_minutes_est or 0.0 for f in self.measured)

    def recoverable_cost_capped(self) -> float:
        """Sum of per-finding estimates, capped at the observed total: estimates for
        different rules overlap (the same minutes can be superseded, re-run and
        rounded), so the plain sum can exceed what was actually billed."""
        total = self.recoverable_cost_total()
        return min(total, float(self.totals.cost)) if self.totals is not None else total

    def recoverable_minutes_capped(self) -> float:
        total = self.recoverable_minutes_total()
        return min(total, float(self.totals.billed_minutes)) if self.totals is not None else total

    def monthly(self, amount: float) -> float:
        return amount * 30.0 / self.window_days if self.window_days else amount

    def as_dict(self) -> dict[str, Any]:
        t = self.totals
        return {
            "schema_version": 1,
            "tool": {"name": "ciburn", "version": __version__},
            "generated_at": self.generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "repo": self.repo,
            "path": self.path,
            "window_days": self.window_days,
            "pricing": {
                "model": self.model.id,
                "model_name": self.model.name,
                "in_effect": self.model.in_effect,
                "source_url": self.model.source_url,
                "fetched_at": self.pricing.fetched_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "currency": self.pricing.currency,
                "public_repo_list_price_equivalent": bool(
                    self.history and self.history.public_repo
                ),
            },
            "history": None
            if self.history is None
            else {
                "since": self.history.since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "until": self.history.until.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "private": self.history.private,
                "runs": len(self.history.runs),
                "runs_with_job_data": sum(1 for r in self.history.runs if r.jobs_fetched),
                "jobs": len(self.history.jobs),
                "jobs_sampled": self.history.jobs_sampled,
                "last_ingest": self.history.last_ingest,
            },
            "totals": None
            if t is None
            else {
                "billed_minutes": t.billed_minutes,
                "raw_seconds": round(t.raw_seconds),
                "rounding_waste_minutes": round(t.rounding_waste_seconds / 60, 1),
                "list_price_cost": round(float(t.cost), 4),
                "list_price_cost_per_month_est": round(self.monthly(float(t.cost)), 4),
                "priced_minutes": t.priced_minutes,
                "unpriced_hosted_minutes": t.unpriced_hosted_minutes,
                "self_hosted_minutes": t.self_hosted_minutes,
                "queue_minutes": round(t.queue_seconds / 60, 1),
                "by_sku": {
                    k: {"minutes": m, "cost": round(float(c), 4)} for k, (m, c) in t.by_sku.items()
                },
                "by_workflow": {
                    k: {"minutes": m, "cost": round(float(c), 4)}
                    for k, (m, c) in t.by_workflow.items()
                },
                "by_event": {
                    k: {"minutes": m, "cost": round(float(c), 4)}
                    for k, (m, c) in t.by_event.items()
                },
                "by_conclusion": {
                    k: {"minutes": m, "cost": round(float(c), 4)}
                    for k, (m, c) in t.by_conclusion.items()
                },
            },
            "included_minutes": None
            if self.included is None
            else {
                "plan": self.included.plan,
                "included_minutes": self.included.included_minutes,
                "drawdown_minutes_est": float(self.included.drawdown_minutes),
                "gross_cost": float(self.included.gross_cost),
                "covered_cost_est": float(self.included.covered_cost),
                "net_cost_est": float(self.included.net_cost),
                "assumption": self.included.assumption,
            },
            "summary": {
                "findings": len(self.findings),
                "measured": len(self.measured),
                "advisory": len(self.advisory),
                "recoverable_minutes_est_sum": round(self.recoverable_minutes_total(), 1),
                "recoverable_cost_est_sum": round(self.recoverable_cost_total(), 4),
                "recoverable_minutes_est_capped": round(self.recoverable_minutes_capped(), 1),
                "recoverable_cost_est_capped": round(self.recoverable_cost_capped(), 4),
                "recoverable_cost_per_month_est_capped": round(
                    self.monthly(self.recoverable_cost_capped()), 4
                ),
                "recoverable_note": (
                    "per-finding estimates overlap; the sum is not additive and the capped "
                    "values are limited to the observed total"
                ),
                "worst_severity": (self.worst_severity() or Severity.LOW).value
                if self.findings
                else None,
            },
            "findings": [f.as_dict() for f in self.findings],
            "notes": self.notes,
            "error": self.error,
        }


def detect_repo(path: Path) -> str | None:
    """``owner/name`` from the git remote ``origin`` of ``path``, if it points at github.com."""
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    m = REMOTE_RE.search(out.stdout.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def run_audit(opts: AuditOptions) -> AuditResult:
    now = opts.now or datetime.now(UTC)
    pricing = Pricing.load()
    model = pricing.model(opts.model_id)
    progress = opts.progress or (lambda _e, _i: None)
    notes: list[str] = []
    if pricing.is_stale(now):
        notes.append(
            f"pricing.yaml was fetched {pricing.age(now).days} days ago; verify rates against the source URLs."
        )
    if not model.in_effect:
        notes.append(
            f"Pricing model {model.id!r} is not in effect ({model.status}); figures are a scenario."
        )

    repo = opts.repo
    if repo is None and opts.path is not None and not opts.no_history:
        repo = detect_repo(opts.path)
        if repo:
            notes.append(f"Repository {repo} detected from the git remote.")
    if repo is not None:
        repo = repo.strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")

    workflows: list[Workflow] = []
    layout: RepoLayout | None = None
    if opts.path is not None:
        workflows = load_workflows(opts.path)
        layout = RepoLayout.from_path(opts.path)

    history: HistoryView | None = None
    ingest_result: IngestResult | None = None
    error: str | None = None
    if repo is not None and not opts.no_history:
        owner, _, name = repo.partition("/")
        store = Store(opts.cache or default_cache_path())
        try:
            if not opts.offline:
                client = GitHubClient(opts.token, log=lambda m: progress("log", {"message": m}))
                if not client.authenticated:
                    notes.append(
                        "No GITHUB_TOKEN: using the unauthenticated limit of 60 requests/hour; "
                        "large repositories will be sampled. Set GITHUB_TOKEN for 5,000/hour."
                    )
                try:
                    ingest_result = ingest(
                        client,
                        store,
                        owner,
                        name,
                        IngestOptions(
                            days=opts.days,
                            max_job_runs=opts.max_job_runs
                            if opts.max_job_runs
                            else (None if client.authenticated else 40),
                            max_run_pages=None if client.authenticated else 2,
                            fetch_commits=opts.fetch_commits and client.authenticated,
                            fetch_timing=True,
                        ),
                        progress=progress,
                        now=now,
                    )
                    notes.extend(ingest_result.warnings[:5])
                finally:
                    client.close()
            if not workflows:
                repo_row = store.get_repo(repo)
                if repo_row is not None:
                    workflows = load_workflow_texts(store.workflow_files(int(repo_row["id"])))
            history = HistoryView.from_store(
                store,
                repo,
                pricing,
                model,
                workflows=workflows,
                window_days=opts.days,
                now=now,
                label_overrides=opts.label_overrides,
            )
            if layout is None:
                layout = history.layout
            if history.public_repo:
                notes.append(
                    "Public repository: standard-runner minutes are free on GitHub. Costs are the "
                    "private-repository list-price equivalent."
                )
            if history.jobs_sampled:
                notes.append(
                    f"Job-level data covers {sum(1 for r in history.runs if r.jobs_fetched)} of "
                    f"{len(history.runs)} runs in the window (sampled)."
                )
            if not history.runs:
                notes.append(f"No workflow runs found in the last {opts.days} days.")
        except NotFoundError:
            error = f"Repository {repo} was not found (private without a token, or misspelled)."
        except AuthError as exc:
            error = f"Authentication failed: {exc}"
        except GitHubError as exc:
            error = f"GitHub API error: {exc}"
        except KeyError as exc:
            error = f"No cached history for {repo} ({exc}); run without --offline first."
        finally:
            store.close()
        if error:
            notes.append("History could not be loaded; only static (advisory) findings are shown.")

    ctx = Context(
        workflows=workflows,
        pricing=pricing,
        model=model,
        layout=layout,
        history=history,
        window_days=opts.days,
        public_repo=history.public_repo if history else None,
        ignore={r.upper() for r in opts.ignore},
    )
    findings = run_rules(ctx, STATIC_RULES)
    findings = join_findings(findings, ctx)
    findings.extend(run_rules(ctx, HISTORY_RULES))
    findings = sort_findings(findings)

    totals = history.totals() if history else None
    included: IncludedMinutesResult | None = None
    if opts.plan and history is not None:
        included = apply_included_minutes(
            pricing, model, opts.plan, (j.price for j in history.jobs)
        )
        notes.append(included.assumption)

    return AuditResult(
        generated_at=now,
        pricing=pricing,
        model=model,
        window_days=opts.days,
        workflows=workflows,
        findings=findings,
        repo=repo,
        path=str(opts.path) if opts.path else None,
        history=history,
        totals=totals,
        included=included,
        ingest=ingest_result,
        notes=notes,
        error=error,
    )


def parse_label_overrides(items: Mapping[str, str] | list[str] | tuple[str, ...]) -> dict[str, str]:
    if isinstance(items, Mapping):
        return dict(items)
    out: dict[str, str] = {}
    for it in items:
        if "=" not in it:
            raise ValueError(f"--label-sku expects LABEL=SKU, got {it!r}")
        k, v = it.split("=", 1)
        out[k.strip()] = v.strip()
    return out
