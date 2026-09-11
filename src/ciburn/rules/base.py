"""Rule framework: context, protocol, helpers."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ciburn.findings import Confidence, Evidence, Finding, Kind, Remediation, Severity
from ciburn.pricing import PriceModel, Pricing
from ciburn.repo_layout import RepoLayout
from ciburn.workflow import Job, Workflow

if TYPE_CHECKING:
    from ciburn.history.view import HistoryView


@dataclass
class Context:
    workflows: list[Workflow]
    pricing: Pricing
    model: PriceModel
    layout: RepoLayout | None = None
    history: HistoryView | None = None
    window_days: int = 90
    public_repo: bool | None = None

    @property
    def parsed_workflows(self) -> list[Workflow]:
        return [w for w in self.workflows if w.parse_error is None]


@runtime_checkable
class Rule(Protocol):
    id: str
    title: str
    severity: Severity
    needs_history: bool

    def run(self, ctx: Context) -> list[Finding]: ...


def run_rules(ctx: Context, rules: Iterable[Rule]) -> list[Finding]:
    out: list[Finding] = []
    for rule in rules:
        if rule.needs_history and ctx.history is None:
            continue
        out.extend(rule.run(ctx))
    return out


def config_evidence(wf: Workflow, job: Job | None = None, **detail: Any) -> Evidence:
    ref = wf.path if job is None else f"{wf.path}#jobs.{job.key}"
    d: dict[str, Any] = dict(detail)
    if job is not None and job.line is not None:
        d.setdefault("line", job.line)
    return Evidence(kind="config", ref=ref, detail=d)


def advisory(
    rule: Rule,
    wf: Workflow,
    job: Job | None,
    message: str,
    remediation: Remediation,
    evidence: Sequence[Evidence],
    *,
    confidence: Confidence = Confidence.MEDIUM,
    severity: Severity | None = None,
    tags: Sequence[str] = (),
) -> Finding:
    return Finding(
        rule_id=rule.id,
        title=rule.title,
        severity=severity or rule.severity,
        confidence=confidence,
        kind=Kind.ADVISORY,
        message=message,
        remediation=remediation,
        workflow=wf.path,
        job=job.key if job else None,
        evidence=list(evidence),
        tags=list(tags),
    )
