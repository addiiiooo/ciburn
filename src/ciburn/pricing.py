"""Pricing engine.

Converts observed job durations into billable minutes and money under a named
pricing model. All rates come from ``data/pricing.yaml``; nothing in this
module hardcodes a price.

Load-bearing rule (verified, see RESEARCH.md): GitHub rounds the minutes and
partial minutes each job uses up to the nearest whole minute. ``billable_minutes``
implements exactly that, and the property tests in ``tests/test_pricing.py`` pin
the boundaries at 0 s / 1 s / 59 s / 60 s / 61 s.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

DEFAULT_MODEL = "2026"
PRICING_MAX_AGE_DAYS = 90
LINUX_STANDARD_SKU = "actions_linux"


class PricingError(ValueError):
    """Raised for malformed pricing data or unknown models/SKUs."""


@dataclass(frozen=True)
class Sku:
    id: str
    per_minute: Decimal
    os: str
    arch: str
    cores: int
    tier: str  # standard | larger | gpu

    @property
    def draws_included_minutes(self) -> bool:
        # "Included minutes cannot be used for larger runners."
        return self.tier == "standard"


@dataclass(frozen=True)
class PriceModel:
    id: str
    name: str
    in_effect: bool
    status: str
    effective_from: str | None
    source_url: str
    skus: Mapping[str, Sku]
    platform_charge_per_minute: Decimal
    platform_charge_included_in_hosted_rates: bool
    self_hosted_per_minute: Decimal
    self_hosted_counts_against_included_minutes: bool
    self_hosted_public_repositories_free: bool = True

    def sku(self, sku_id: str) -> Sku:
        try:
            return self.skus[sku_id]
        except KeyError as exc:
            raise PricingError(f"unknown SKU {sku_id!r} in model {self.id!r}") from exc


@dataclass(frozen=True)
class RunnerClass:
    """How a job's runner is classified for pricing."""

    kind: str  # "hosted" | "self_hosted_or_custom"
    sku: str | None  # None when the SKU cannot be determined
    label: str | None  # the label that matched, if any

    @property
    def priced(self) -> bool:
        return self.sku is not None


@dataclass(frozen=True)
class JobPrice:
    raw_seconds: float
    billed_minutes: int
    rounding_waste_seconds: float
    rate_per_minute: Decimal
    cost: Decimal
    priced: bool
    sku: str | None
    kind: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw_seconds": self.raw_seconds,
            "billed_minutes": self.billed_minutes,
            "rounding_waste_seconds": self.rounding_waste_seconds,
            "rate_per_minute": float(self.rate_per_minute),
            "cost": float(self.cost),
            "priced": self.priced,
            "sku": self.sku,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class LabelRule:
    pattern: re.Pattern[str]
    sku: str


@dataclass
class Pricing:
    """The parsed pricing.yaml."""

    fetched_at: datetime
    currency: str
    rounding_statement: str
    rounding_source_url: str
    included_minutes: Mapping[str, int]
    drawdown_rule_verified: bool
    default_job_timeout_minutes: int
    ubuntu_slim_job_timeout_minutes: int
    max_matrix_jobs: int
    label_rules: Sequence[LabelRule]
    models: Mapping[str, PriceModel]
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    # -- loading -----------------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> Pricing:
        if path is None:
            with resources.files("ciburn.data").joinpath("pricing.yaml").open("rb") as fh:
                data = yaml.safe_load(fh)
        else:
            with path.open("rb") as fh:
                data = yaml.safe_load(fh)
        if not isinstance(data, dict):
            raise PricingError("pricing.yaml must be a mapping")
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Pricing:
        try:
            fetched_at = _parse_ts(str(data["fetched_at"]))
            rounding = data["rounding"]
            included = data["included_minutes"]
            limits = data["limits"]
            label_block = data["label_patterns"]
            models_raw = data["models"]
        except KeyError as exc:
            raise PricingError(f"pricing.yaml missing key {exc}") from exc

        models: dict[str, PriceModel] = {}
        # two passes so `inherits_skus_from` can reference another model
        pending = dict(models_raw)
        while pending:
            progressed = False
            for mid, m in list(pending.items()):
                parent = m.get("inherits_skus_from")
                if parent is not None and parent not in models:
                    if parent not in models_raw:
                        raise PricingError(f"model {mid!r} inherits from unknown model {parent!r}")
                    continue
                skus = models[parent].skus if parent else _parse_skus(m.get("skus", {}))
                models[str(mid)] = PriceModel(
                    id=str(mid),
                    name=str(m["name"]),
                    in_effect=bool(m.get("in_effect", False)),
                    status=str(
                        m.get("status", "in_effect" if m.get("in_effect") else "historical")
                    ),
                    effective_from=(
                        str(m["effective_from"]) if m.get("effective_from") is not None else None
                    ),
                    source_url=str(m["source_url"]),
                    skus=skus,
                    platform_charge_per_minute=_dec(m.get("platform_charge_per_minute", 0)),
                    platform_charge_included_in_hosted_rates=bool(
                        m.get("platform_charge_included_in_hosted_rates", True)
                    ),
                    self_hosted_per_minute=_dec(m.get("self_hosted_per_minute", 0)),
                    self_hosted_counts_against_included_minutes=bool(
                        m.get("self_hosted_counts_against_included_minutes", False)
                    ),
                    self_hosted_public_repositories_free=bool(
                        m.get("self_hosted_public_repositories_free", True)
                    ),
                )
                del pending[mid]
                progressed = True
            if not progressed:
                raise PricingError("circular inherits_skus_from in pricing.yaml")

        label_rules = [
            LabelRule(re.compile(str(r["pattern"]), re.IGNORECASE), str(r["sku"]))
            for r in label_block["rules"]
        ]
        return cls(
            fetched_at=fetched_at,
            currency=str(data.get("currency", "USD")),
            rounding_statement=str(rounding["statement"]),
            rounding_source_url=str(rounding["source_url"]),
            included_minutes={str(k): int(v) for k, v in included["plans"].items()},
            drawdown_rule_verified=bool(included.get("drawdown_rule_verified", False)),
            default_job_timeout_minutes=int(limits["default_job_timeout_minutes"]),
            ubuntu_slim_job_timeout_minutes=int(limits["ubuntu_slim_job_timeout_minutes"]),
            max_matrix_jobs=int(limits["max_matrix_jobs"]),
            label_rules=label_rules,
            models=models,
            raw=data,
        )

    # -- queries -------------------------------------------------------------

    def age(self, now: datetime | None = None) -> timedelta:
        now = now or datetime.now(UTC)
        return now - self.fetched_at

    def is_stale(self, now: datetime | None = None) -> bool:
        return self.age(now) > timedelta(days=PRICING_MAX_AGE_DAYS)

    def model(self, model_id: str = DEFAULT_MODEL) -> PriceModel:
        try:
            return self.models[model_id]
        except KeyError as exc:
            raise PricingError(
                f"unknown pricing model {model_id!r}; available: {', '.join(self.models)}"
            ) from exc

    def classify(
        self,
        labels: Iterable[str],
        runner_name: str | None = None,
        overrides: Mapping[str, str] | None = None,
    ) -> RunnerClass:
        """Classify a job's runner from its labels and runner name.

        See DECISIONS.md D007. ``overrides`` maps a custom label to a SKU id.
        """
        labels = [str(lb) for lb in labels]
        if overrides:
            for lb in labels:
                if lb in overrides:
                    return RunnerClass(kind="hosted", sku=overrides[lb], label=lb)
        for lb in labels:
            for rule in self.label_rules:
                if rule.pattern.match(lb):
                    return RunnerClass(kind="hosted", sku=rule.sku, label=lb)
        hosted_name = bool(runner_name) and str(runner_name).startswith(
            ("GitHub Actions", "GitHub-hosted", "Hosted Agent")
        )
        if hosted_name:
            # GitHub-hosted, but a larger runner with an org-defined label: unknown SKU
            return RunnerClass(kind="hosted", sku=None, label=labels[0] if labels else None)
        return RunnerClass(
            kind="self_hosted_or_custom", sku=None, label=labels[0] if labels else None
        )


# -- arithmetic ---------------------------------------------------------------


def billable_minutes(seconds: float | int) -> int:
    """GitHub's per-job rounding: partial minutes round *up* to a whole minute.

    Negative durations (clock skew) are treated as zero.
    """
    if math.isnan(seconds) or seconds <= 0:
        return 0
    return math.ceil(seconds / 60.0)


def rounding_waste_seconds(seconds: float | int) -> float:
    """Seconds billed but not used, for one job."""
    if math.isnan(seconds) or seconds <= 0:
        return 0.0
    return float(billable_minutes(seconds) * 60 - seconds)


def price_minutes(model: PriceModel, sku_id: str, minutes: int) -> Decimal:
    if minutes < 0:
        raise ValueError("minutes must be >= 0")
    return _money(model.sku(sku_id).per_minute * minutes)


def price_job(
    model: PriceModel,
    runner: RunnerClass,
    seconds: float | int,
    *,
    public_repo: bool = False,
) -> JobPrice:
    """Price one job under ``model``.

    ``public_repo`` prices at $0 for standard hosted runners and self-hosted
    runners (GitHub: free) and keeps the list price for larger runners
    (GitHub: "always charged for"). Callers that want the list-price
    equivalent for a public repository pass ``public_repo=False``.
    """
    minutes = billable_minutes(seconds)
    waste = rounding_waste_seconds(seconds)
    raw = 0.0 if math.isnan(seconds) else float(max(seconds, 0))
    if runner.kind == "hosted":
        if runner.sku is None:
            return JobPrice(raw, minutes, waste, Decimal(0), Decimal(0), False, None, runner.kind)
        sku = model.sku(runner.sku)
        rate = sku.per_minute
        if public_repo and sku.tier == "standard":
            rate = Decimal(0)
        return JobPrice(
            raw, minutes, waste, rate, _money(rate * minutes), True, sku.id, runner.kind
        )
    rate = model.self_hosted_per_minute
    if public_repo and model.self_hosted_public_repositories_free:
        rate = Decimal(0)
    priced = model.self_hosted_per_minute > 0
    return JobPrice(raw, minutes, waste, rate, _money(rate * minutes), priced, None, runner.kind)


@dataclass
class IncludedMinutesResult:
    plan: str
    included_minutes: int
    drawdown_minutes: Decimal
    gross_cost: Decimal
    covered_cost: Decimal
    net_cost: Decimal
    assumption: str


def apply_included_minutes(
    pricing: Pricing,
    model: PriceModel,
    plan: str,
    priced_jobs: Iterable[JobPrice],
) -> IncludedMinutesResult:
    """Offset gross cost by a plan's included minutes (DECISIONS.md D005).

    Standard-runner minutes draw down the quota at the ratio
    ``sku_rate / actions_linux_rate``. That ratio is an assumption and is
    returned in ``assumption`` so every report can print it.
    """
    if plan not in pricing.included_minutes:
        raise PricingError(
            f"unknown plan {plan!r}; available: {', '.join(pricing.included_minutes)}"
        )
    quota = pricing.included_minutes[plan]
    linux_rate = model.sku(LINUX_STANDARD_SKU).per_minute
    gross = Decimal(0)
    eligible_cost = Decimal(0)
    drawdown = Decimal(0)
    for jp in priced_jobs:
        gross += jp.cost
        standard = jp.sku is not None and model.sku(jp.sku).tier == "standard"
        self_hosted_eligible = (
            jp.sku is None
            and jp.kind == "self_hosted_or_custom"
            and model.self_hosted_counts_against_included_minutes
        )
        if standard or self_hosted_eligible:
            eligible_cost += jp.cost
            drawdown += Decimal(jp.billed_minutes) * (jp.rate_per_minute / linux_rate)
    if drawdown <= 0:
        covered = Decimal(0)
    else:
        fraction = min(Decimal(1), Decimal(quota) / drawdown)
        covered = _money(eligible_cost * fraction)
    return IncludedMinutesResult(
        plan=plan,
        included_minutes=quota,
        drawdown_minutes=_money(drawdown),
        gross_cost=_money(gross),
        covered_cost=covered,
        net_cost=_money(gross - covered),
        assumption=(
            "assumption: included minutes drawn down at list-price ratio (sku_rate / actions_linux rate); "
            "GitHub does not document the current drawdown rule for non-Linux standard runners"
        ),
    )


# -- helpers ------------------------------------------------------------------


def _parse_skus(raw: Mapping[str, Any]) -> dict[str, Sku]:
    out: dict[str, Sku] = {}
    for sid, s in raw.items():
        if s.get("unverified"):
            # excluded from cost math by design
            continue
        out[str(sid)] = Sku(
            id=str(sid),
            per_minute=_dec(s["per_minute"]),
            os=str(s["os"]),
            arch=str(s.get("arch", "any")),
            cores=int(s.get("cores", 0)),
            tier=str(s.get("tier", "standard")),
        )
    return out


def _dec(v: Any) -> Decimal:
    d = Decimal(str(v))
    if d < 0:
        raise PricingError(f"negative rate in pricing.yaml: {v!r}")
    return d


def _money(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def _parse_ts(s: str) -> datetime:
    s = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt
