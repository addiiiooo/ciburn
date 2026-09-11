"""The findings model shared by every rule, the join, and every reporter."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return {"low": 1, "medium": 2, "high": 3}[self.value]


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return {"low": 1, "medium": 2, "high": 3}[self.value]


class Kind(StrEnum):
    """``measured`` findings carry observed minutes from the repository's own
    run history. ``advisory`` findings come from configuration alone and carry
    no numbers. Reporters must keep the two visually separate."""

    MEASURED = "measured"
    ADVISORY = "advisory"


@dataclass
class Evidence:
    """One row a finding derives from. ``ref`` is a stable locator."""

    kind: str  # config | run | job | step | workflow
    ref: str  # e.g. ".github/workflows/ci.yml#jobs.test", "run:123", "job:456"
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Remediation:
    summary: str
    patch: dict[str, Any] = field(default_factory=dict)  # machine-readable hint for `fix`
    docs_url: str | None = None


@dataclass
class Finding:
    rule_id: str
    title: str
    severity: Severity
    confidence: Confidence
    kind: Kind
    message: str
    remediation: Remediation
    workflow: str | None = None  # workflow path
    job: str | None = None  # job key (config) or API job-name prefix
    observed_minutes: float | None = None  # billed minutes observed in the window
    observed_cost: float | None = None  # list price under the selected model
    observed_runs: int | None = None
    recoverable_minutes_est: float | None = None
    recoverable_cost_est: float | None = None
    estimate_method: str | None = None
    window_days: int | None = None
    evidence: list[Evidence] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        d["confidence"] = self.confidence.value
        d["kind"] = self.kind.value
        return d

    @property
    def location(self) -> str:
        if self.workflow and self.job:
            return f"{self.workflow}#{self.job}"
        return self.workflow or "(repository)"


def sort_findings(findings: list[Finding]) -> list[Finding]:
    """Measured first, then by money, then severity, then rule id."""

    def key(f: Finding) -> tuple[int, float, float, int, str, str]:
        return (
            0 if f.kind is Kind.MEASURED else 1,
            -(f.recoverable_cost_est or 0.0),
            -(f.observed_cost or 0.0),
            -f.severity.rank,
            f.rule_id,
            f.location,
        )

    return sorted(findings, key=key)
