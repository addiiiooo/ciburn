"""Formatting helpers shared by reporters."""

from __future__ import annotations

from ciburn.audit import AuditResult
from ciburn.findings import Finding


def money(x: float | None) -> str:
    if x is None:
        return "—"
    if x == 0:
        return "$0"
    if abs(x) < 0.01:
        return "<$0.01"
    return f"${x:,.2f}"


def minutes(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x:,.0f} min"


def observed_cell(f: Finding) -> str:
    if f.observed_minutes is None:
        return "—"
    s = minutes(f.observed_minutes)
    if f.observed_cost:
        s += f" · {money(f.observed_cost)}"
    if f.observed_runs:
        s += f" · n={f.observed_runs}"
    return s


def recoverable_cell(f: Finding, result: AuditResult) -> str:
    if f.recoverable_minutes_est is None and f.recoverable_cost_est is None:
        return "not estimated"
    parts: list[str] = []
    if f.recoverable_minutes_est is not None:
        parts.append(minutes(f.recoverable_minutes_est))
    if f.recoverable_cost_est is not None:
        parts.append(
            f"{money(f.recoverable_cost_est)} (≈{money(result.monthly(f.recoverable_cost_est))}/mo)"
        )
    return " · ".join(parts)


def headline(result: AuditResult) -> str:
    where = result.repo or result.path or "(unknown)"
    span = ""
    if result.history:
        span = f" · {result.history.since:%Y-%m-%d} → {result.history.until:%Y-%m-%d}"
    return (
        f"ciburn — {where} · last {result.window_days} days{span} · pricing model {result.model.id}"
    )


def totals_lines(result: AuditResult) -> list[str]:
    t = result.totals
    if t is None or result.history is None:
        return ["No run history loaded (static analysis only)."]
    lines = [
        f"Observed: {t.runs:,} runs · {t.jobs:,} jobs · {t.billed_minutes:,} billed minutes · "
        f"{money(float(t.cost))} list price (≈{money(result.monthly(float(t.cost)))}/month)",
    ]
    skus = sorted(t.by_sku.items(), key=lambda kv: -kv[1][0])
    if skus:
        lines.append(
            "By runner: " + " · ".join(f"{k} {m:,} min {money(float(c))}" for k, (m, c) in skus[:6])
        )
    extras: list[str] = []
    if t.unpriced_hosted_minutes:
        extras.append(
            f"{t.unpriced_hosted_minutes:,} min on GitHub-hosted runners with unknown SKU (unpriced; use --label-sku)"
        )
    if t.self_hosted_minutes:
        extras.append(
            f"{t.self_hosted_minutes:,} min on self-hosted/custom runners (priced at $0 under model {result.model.id})"
        )
    if extras:
        lines.append("Unpriced: " + "; ".join(extras))
    if t.billed_minutes:
        lines.append(
            f"Rounding: per-job round-up to whole minutes added {t.rounding_waste_seconds / 60:,.0f} min "
            f"({t.rounding_waste_seconds / 60 / t.billed_minutes:.0%} of billed minutes)"
        )
    if result.included:
        i = result.included
        lines.append(
            f"Plan {i.plan}: {i.included_minutes:,} included minutes cover ≈{money(float(i.covered_cost))} of "
            f"{money(float(i.gross_cost))}; net ≈{money(float(i.net_cost))} ({i.assumption})"
        )
    biggest = max(result.measured, key=lambda f: f.recoverable_cost_est or 0.0, default=None)
    if biggest is not None and (biggest.recoverable_cost_est or biggest.recoverable_minutes_est):
        lines.append(
            "Recoverable: per-finding estimates overlap and are not additive; the largest single one is "
            f"{biggest.rule_id} at {minutes(biggest.recoverable_minutes_est)} · "
            f"{money(biggest.recoverable_cost_est)} (≈{money(result.monthly(biggest.recoverable_cost_est or 0.0))}/month)"
        )
    return lines


def top_groups(result: AuditResult, n: int = 8) -> list[tuple[str, str, int, int, float, float]]:
    """(workflow, job, jobs, billed minutes, cost, p50 minutes) for the biggest job groups."""
    if result.history is None:
        return []
    from ciburn.stats import median

    rows = []
    for (path, base), jobs in result.history.job_groups().items():
        mins = sum(j.billed_minutes for j in jobs)
        cost = float(sum(float(j.cost) for j in jobs))
        p50 = median([j.raw_seconds / 60 for j in jobs if j.raw_seconds > 0])
        rows.append((path, base, len(jobs), mins, cost, p50))
    rows.sort(key=lambda r: -r[3])
    return rows[:n]
