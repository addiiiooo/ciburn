"""`ciburn price`: totals under one or all pricing models."""

from __future__ import annotations

import json
from typing import Any

from rich import box
from rich.console import Console
from rich.table import Table

from ciburn.audit import AuditOptions, run_audit
from ciburn.history.view import HistoryView
from ciburn.pricing import apply_included_minutes
from ciburn.report.common import money


def run_price(opts: AuditOptions, *, compare: bool, fmt: str) -> int:
    result = run_audit(opts)
    if result.history is None:
        Console(stderr=True).print(
            f"[red]no history: {result.error or 'give --repo or run inside a GitHub checkout'}[/red]"
        )
        return 2
    pricing = result.pricing
    view: HistoryView = result.history
    model_ids = list(pricing.models) if compare else [opts.model_id]
    rows: list[dict[str, Any]] = []
    for mid in model_ids:
        model = pricing.model(mid)
        view.reprice(pricing, model, opts.label_overrides)
        t = view.totals()
        gross = float(t.cost)
        net = None
        if opts.plan:
            inc = apply_included_minutes(pricing, model, opts.plan, (j.price for j in view.jobs))
            net = float(inc.net_cost)
        rows.append(
            {
                "model": mid,
                "name": model.name,
                "in_effect": model.in_effect,
                "status": model.status,
                "billed_minutes": t.billed_minutes,
                "priced_minutes": t.priced_minutes,
                "self_hosted_minutes": t.self_hosted_minutes,
                "unpriced_hosted_minutes": t.unpriced_hosted_minutes,
                "list_price": round(gross, 4),
                "per_month_est": round(result.monthly(gross), 4),
                "net_after_plan_est": None if net is None else round(net, 4),
                "by_sku": {
                    k: {"minutes": m, "cost": round(float(c), 4)} for k, (m, c) in t.by_sku.items()
                },
            }
        )
    view.reprice(pricing, pricing.model(opts.model_id), opts.label_overrides)
    if fmt == "json":
        print(
            json.dumps(
                {
                    "repo": result.repo,
                    "window_days": result.window_days,
                    "public_repo": view.public_repo,
                    "models": rows,
                    "notes": result.notes,
                },
                indent=2,
            )
        )
        return 0
    console = Console()
    console.print(
        f"[bold]{result.repo}[/bold] · last {result.window_days} days · {len(view.runs):,} runs · {len(view.jobs):,} jobs"
    )
    if view.public_repo:
        console.print(
            "[dim]Public repository: standard-runner minutes are free on GitHub; figures are the private list-price equivalent.[/dim]"
        )
    tbl = Table(box=box.SIMPLE)
    for col in (
        "model",
        "status",
        "billed min",
        "priced min",
        "self-hosted min",
        "list price",
        "≈ / month",
        "net after plan",
    ):
        tbl.add_column(
            col,
            justify="right"
            if "min" in col or "price" in col or "month" in col or "plan" in col
            else "left",
        )
    for r in rows:
        tbl.add_row(
            str(r["model"]),
            "in effect" if r["in_effect"] else str(r["status"]),
            f"{r['billed_minutes']:,}",
            f"{r['priced_minutes']:,}",
            f"{r['self_hosted_minutes']:,}",
            money(float(r["list_price"])),
            money(float(r["per_month_est"])),
            "—" if r["net_after_plan_est"] is None else money(float(r["net_after_plan_est"])),
        )
    console.print(tbl)
    if len(rows) > 1:
        base = next((r for r in rows if r["model"] == "2025"), rows[0])
        cur = next((r for r in rows if r["model"] == "2026"), rows[-1])
        delta = float(cur["list_price"]) - float(base["list_price"])
        pct = (delta / float(base["list_price"]) * 100) if float(base["list_price"]) else 0.0
        console.print(
            f"2026 vs 2025 list price: {money(delta)} ({pct:+.1f}%) for the same {cur['billed_minutes']:,} billed minutes."
        )
    for n in result.notes:
        console.print(f"[dim]note: {n}[/dim]", highlight=False)
    return 0
