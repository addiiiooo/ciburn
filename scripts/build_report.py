"""Build the public dataset and REPORT.md from a corpus scan.

Reads ``<workdir>/corpus.sqlite`` and ``<workdir>/sample.json`` (produced by
``scripts/corpus_scan.py``), runs the full ciburn analysis on every scanned
repository, and writes:

    dataset/repos.{csv,parquet}       one row per sampled repository
    dataset/workflows.{csv,parquet}   one row per workflow file (static facts)
    dataset/runs.{csv,parquet}        one row per listed run (<= 200 per repo)
    dataset/jobs.{csv,parquet}        one row per sampled job (<= 8 runs per repo)
    dataset/findings.{csv,parquet}    one row per finding per repository
    dataset/summary.json              every number quoted in REPORT.md
    dataset/sample.json               the sample selection record
    REPORT.md                         generated from summary.json + fixed prose

Everything in REPORT.md is regenerable by re-running this script; nothing is
typed in by hand. Only aggregate data and public repository names appear.

Usage: python scripts/build_report.py --workdir corpus --out dataset --report REPORT.md
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from ciburn import __version__
from ciburn.findings import Kind
from ciburn.history.store import Store
from ciburn.history.view import HistoryView
from ciburn.join import join_findings
from ciburn.pricing import Pricing, price_job
from ciburn.repo_layout import RepoLayout
from ciburn.rules import HISTORY_RULES, STATIC_RULES, Context, run_rules
from ciburn.rules.static_jobs import _is_install_step, _job_has_cache
from ciburn.workflow import Workflow, load_workflow_texts

ICSE = {
    "caching_paid_tier_pct": 32.9,
    "custom_timeout_paid_tier_pct": 14.0,
    "failed_runs_pct": 17.4,
    "failed_vm_time_pct": 30.9,
    "scheduled_runs_pct": 29.8,
    "scheduled_vm_time_pct": 15.4,
    "scheduled_failure_pct": 13.0,
    "source": "https://software-lab.org/publications/icse2024_workflows.pdf",
}


def pct(n: float, d: float) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def q(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    k = max(0, min(len(xs) - 1, round(p / 100 * (len(xs) - 1))))
    return float(xs[k])


def analyse_repo(
    store: Store, pricing: Pricing, full_name: str, now: datetime
) -> dict[str, Any] | None:
    repo = store.get_repo(full_name)
    if repo is None:
        return None
    rid = int(repo["id"])
    status = store.get_state(rid, "corpus_status") or {}
    if status.get("status") != "done":
        return None
    workflows: list[Workflow] = load_workflow_texts(store.workflow_files(rid))
    layout_state = store.get_state(rid, "layout") or {}
    layout = RepoLayout(
        top_level_dirs=set(layout_state.get("top_level_dirs", [])),
        markdown_files=int(layout_state.get("markdown_files", 0)),
        total_files=int(layout_state.get("total_files", 0)),
    )
    model = pricing.model("2026")
    view = HistoryView.from_store(
        store, full_name, pricing, model, workflows=workflows, window_days=90, now=now
    )
    ctx = Context(
        workflows=workflows,
        pricing=pricing,
        model=model,
        layout=layout,
        history=view,
        window_days=90,
    )
    findings = join_findings(run_rules(ctx, STATIC_RULES), ctx)
    findings.extend(run_rules(ctx, HISTORY_RULES))
    last = store.get_state(rid, "last_ingest") or {}
    return {
        "repo_row": repo,
        "rid": rid,
        "status": status,
        "workflows": workflows,
        "view": view,
        "findings": findings,
        "layout": layout,
        "last_ingest": last,
        "model_2025": pricing.model("2025"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="corpus")
    ap.add_argument("--out", default="dataset")
    ap.add_argument("--report", default="REPORT.md")
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="analyse only the first N sampled repositories (debug)",
    )
    args = ap.parse_args()
    workdir, out = Path(args.workdir), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    sample = json.loads((workdir / "sample.json").read_text(encoding="utf-8"))
    # snapshot the scanner's database so a running scan never blocks or skews the analysis
    snapshot = out / ".corpus-snapshot.sqlite"
    src = sqlite3.connect(str(workdir / "corpus.sqlite"))
    dst = sqlite3.connect(str(snapshot))
    src.backup(dst)
    src.close()
    dst.close()
    store = Store(snapshot)
    pricing = Pricing.load()
    now = datetime.now(UTC)

    repo_rows: list[dict[str, Any]] = []
    wf_rows: list[dict[str, Any]] = []
    run_rows: list[dict[str, Any]] = []
    job_rows: list[dict[str, Any]] = []
    finding_rows: list[dict[str, Any]] = []
    per_repo_rule_minutes: dict[str, list[float]] = defaultdict(list)

    entries = sample["repos"][: args.limit] if args.limit else sample["repos"]
    for i, entry in enumerate(entries, 1):
        name = entry["full_name"]
        if i % 100 == 0:
            print(f"  analysed {i}/{len(entries)}", flush=True)
        a = analyse_repo(store, pricing, name, now)
        base = {
            "repo": name,
            "language": entry["language"],
            "star_bucket": entry["star_bucket"],
            "stars_at_selection": entry["stars_at_selection"],
            "rank_in_cell": entry["rank_in_cell"],
        }
        if a is None:
            row = store.get_repo(name)
            st = (
                (store.get_state(int(row["id"]), "corpus_status") or {}).get("status")
                if row
                else "not_scanned"
            )
            repo_rows.append({**base, "scan_status": st or "not_scanned"})
            continue
        view: HistoryView = a["view"]
        wfs: list[Workflow] = a["workflows"]
        parsed = [w for w in wfs if w.parse_error is None]
        jobs_cfg = [j for w in parsed for j in w.jobs.values() if not j.is_reusable_call]
        any_cache = any(_job_has_cache(j) for j in jobs_cfg)
        install_jobs = [j for j in jobs_cfg if any(_is_install_step(s) for s in j.steps)]
        any_timeout = any(j.timeout_minutes is not None for j in jobs_cfg) or any(
            isinstance(w.raw, dict) and w.raw.get("timeout-minutes") is not None for w in parsed
        )
        all_timeout = bool(jobs_cfg) and all(j.timeout_minutes is not None for j in jobs_cfg)
        t = view.totals()
        cost_2025 = sum(
            (price_job(a["model_2025"], j.runner, j.raw_seconds).cost for j in view.jobs),
            Decimal(0),
        )
        runs_total = int(a["last_ingest"].get("runs_total_in_window", len(view.runs)))
        runs_with_jobs = [r for r in view.runs if r.jobs_fetched and r.jobs]
        avg_min_per_run = (
            (sum(r.billed_minutes for r in runs_with_jobs) / len(runs_with_jobs))
            if runs_with_jobs
            else 0.0
        )
        sched = [r for r in view.runs if r.event == "schedule"]
        failed_runs = [r for r in view.runs if r.conclusion in ("failure", "timed_out")]
        repo_rows.append(
            {
                **base,
                "scan_status": "done",
                "private": view.private,
                "default_branch": view.default_branch,
                "workflow_files": len(wfs),
                "workflow_files_parsed": len(parsed),
                "config_jobs": len(jobs_cfg),
                "has_workflows": bool(wfs),
                "uses_cache_any_job": any_cache,
                "jobs_with_install_steps": len(install_jobs),
                "install_jobs_with_cache": sum(1 for j in install_jobs if _job_has_cache(j)),
                "timeout_any_job": any_timeout,
                "timeout_all_jobs": all_timeout,
                "concurrency_cancel_any_pr_workflow": any(
                    w.has_event("pull_request", "pull_request_target")
                    and isinstance(w.concurrency, dict)
                    and w.concurrency.get("cancel-in-progress") is not False
                    and w.concurrency.get("cancel-in-progress") is not None
                    for w in parsed
                ),
                "pr_workflows": sum(
                    1 for w in parsed if w.has_event("pull_request", "pull_request_target")
                ),
                "scheduled_workflows": sum(1 for w in parsed if w.crons),
                "runs_total_in_window_90d": runs_total,
                "runs_listed": len(view.runs),
                "runs_failed_listed": len(failed_runs),
                "runs_scheduled_listed": len(sched),
                "runs_with_job_data": len(runs_with_jobs),
                "jobs_sampled": len(view.jobs),
                "billed_minutes_sampled": t.billed_minutes,
                "raw_seconds_sampled": round(t.raw_seconds),
                "rounding_waste_minutes_sampled": round(t.rounding_waste_seconds / 60, 1),
                "list_price_2026_sampled": round(float(t.cost), 4),
                "list_price_2025_sampled": round(float(cost_2025), 4),
                "priced_minutes_sampled": t.priced_minutes,
                "self_hosted_minutes_sampled": t.self_hosted_minutes,
                "unpriced_hosted_minutes_sampled": t.unpriced_hosted_minutes,
                "avg_billed_minutes_per_run_sampled": round(avg_min_per_run, 2),
                "est_billed_minutes_90d": round(avg_min_per_run * runs_total),
                "est_list_price_2026_90d": round(
                    float(t.cost) / t.billed_minutes * avg_min_per_run * runs_total, 2
                )
                if t.billed_minutes
                else 0.0,
                "markdown_files": a["layout"].markdown_files,
                "top_level_dirs": len(a["layout"].top_level_dirs),
                "scanned_at": a["status"].get("at"),
                "api_requests": a["status"].get("requests"),
            }
        )
        for w in wfs:
            wf_rows.append(
                {
                    "repo": name,
                    "path": w.path,
                    "parse_error": w.parse_error,
                    "name": w.name,
                    "events": ",".join(w.events),
                    "crons": ";".join(w.crons),
                    "jobs": len(w.jobs),
                    "reusable_call_jobs": sum(1 for j in w.jobs.values() if j.is_reusable_call),
                    "jobs_with_timeout": sum(
                        1 for j in w.jobs.values() if j.timeout_minutes is not None
                    ),
                    "jobs_with_matrix": sum(1 for j in w.jobs.values() if j.matrix is not None),
                    "max_matrix_legs": max(
                        (j.matrix_legs or 0 for j in w.jobs.values()), default=0
                    ),
                    "concurrency_cancel": isinstance(w.concurrency, dict)
                    and bool(w.concurrency.get("cancel-in-progress")),
                    "has_path_filter": any(
                        isinstance(c, dict) and ("paths" in c or "paths-ignore" in c)
                        for c in w.on.values()
                    ),
                    "jobs_with_cache": sum(1 for j in w.jobs.values() if _job_has_cache(j)),
                    "jobs_with_install": sum(
                        1 for j in w.jobs.values() if any(_is_install_step(s) for s in j.steps)
                    ),
                    "runs_listed": sum(1 for r in view.runs if r.path == w.path),
                }
            )
        for r in view.runs:
            run_rows.append(
                {
                    "repo": name,
                    "run_id": r.id,
                    "workflow_path": r.path,
                    "event": r.event,
                    "status": r.status,
                    "conclusion": r.conclusion,
                    "run_attempt": r.run_attempt,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "wall_seconds": r.wall_seconds,
                    "jobs_fetched": r.jobs_fetched,
                }
            )
        for j in view.jobs:
            job_rows.append(
                {
                    "repo": name,
                    "job_id": j.id,
                    "run_id": j.run_id,
                    "run_attempt": j.run_attempt,
                    "workflow_path": j.workflow_path,
                    "job": j.base_name,
                    "event": j.event,
                    "conclusion": j.conclusion,
                    "raw_seconds": round(j.raw_seconds, 1),
                    "billed_minutes": j.billed_minutes,
                    "rounding_waste_seconds": round(j.price.rounding_waste_seconds, 1),
                    "queue_seconds": round(j.queue_seconds, 1),
                    "runner_kind": j.runner.kind,
                    "sku": j.runner.sku,
                    "labels": ";".join(j.labels),
                    "cost_2026": float(j.cost),
                    "cost_2025": float(price_job(a["model_2025"], j.runner, j.raw_seconds).cost),
                    "steps": len(j.steps),
                }
            )
        for f in a["findings"]:
            finding_rows.append(
                {
                    "repo": name,
                    "rule": f.rule_id,
                    "workflow": f.workflow,
                    "job": f.job,
                    "kind": f.kind.value,
                    "severity": f.severity.value,
                    "confidence": f.confidence.value,
                    "observed_minutes": f.observed_minutes,
                    "observed_cost_2026": f.observed_cost,
                    "recoverable_minutes_est": f.recoverable_minutes_est,
                    "recoverable_cost_2026_est": f.recoverable_cost_est,
                    "estimate_method": f.estimate_method,
                }
            )
            if f.kind is Kind.MEASURED and f.recoverable_minutes_est:
                per_repo_rule_minutes[f.rule_id].append(f.recoverable_minutes_est)

    frames = {
        "repos": pd.DataFrame(repo_rows),
        "workflows": pd.DataFrame(wf_rows),
        "runs": pd.DataFrame(run_rows),
        "jobs": pd.DataFrame(job_rows),
        "findings": pd.DataFrame(finding_rows),
    }
    for key, df in frames.items():
        df.to_csv(out / f"{key}.csv", index=False)
        df.to_parquet(out / f"{key}.parquet", index=False)
    (out / "sample.json").write_text(json.dumps(sample, indent=1), encoding="utf-8")

    summary = build_summary(frames, sample, now)
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    Path(args.report).write_text(render_report(summary), encoding="utf-8")
    write_schema(out)
    store.close()
    snapshot.unlink(missing_ok=True)
    print(
        f"repos scanned: {summary['sample']['scanned']} / {summary['sample']['selected']}; wrote {out}/ and {args.report}"
    )
    return 0


def build_summary(
    frames: dict[str, pd.DataFrame], sample: dict[str, Any], now: datetime
) -> dict[str, Any]:
    repos, jobs, runs, findings, wfs = (
        frames["repos"],
        frames["jobs"],
        frames["runs"],
        frames["findings"],
        frames["workflows"],
    )
    done = repos[repos["scan_status"] == "done"] if "scan_status" in repos else repos.iloc[0:0]
    with_wf = done[done["has_workflows"]] if len(done) else done
    n_wf = len(with_wf)
    s: dict[str, Any] = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ciburn_version": __version__,
        "sample": {
            "selected": len(sample["repos"]),
            "selected_at": sample["selected_at"],
            "scanned": len(done),
            "with_workflows": int(n_wf),
            "without_workflows": int(len(done) - n_wf),
            "not_scanned_or_failed": int(len(repos) - len(done)),
            "per_cell": sample["per_cell"],
            "languages": sample["languages"],
            "star_buckets": sample["star_buckets"],
            "pushed_since": sample["pushed_since"],
            "window_days": sample["window_days"],
            "by_language": {
                str(k): int(v) for k, v in done["language"].value_counts().sort_index().items()
            }
            if len(done)
            else {},
            "by_star_bucket": {
                str(k): int(v) for k, v in done["star_bucket"].value_counts().sort_index().items()
            }
            if len(done)
            else {},
        },
    }
    if n_wf == 0:
        return s
    jobs_priced = jobs[jobs["runner_kind"] == "hosted"] if len(jobs) else jobs
    billed = float(jobs["billed_minutes"].sum()) if len(jobs) else 0.0
    raw_min = float(jobs["raw_seconds"].sum()) / 60 if len(jobs) else 0.0
    waste_min = float(jobs["rounding_waste_seconds"].sum()) / 60 if len(jobs) else 0.0
    per_repo_waste = [
        pct(float(w), float(bm))
        for w, bm in zip(
            with_wf["rounding_waste_minutes_sampled"].tolist(),
            with_wf["billed_minutes_sampled"].tolist(),
            strict=True,
        )
        if float(bm) > 0
    ]
    failed_jobs = jobs[jobs["conclusion"].isin(["failure", "timed_out"])] if len(jobs) else jobs
    sched_jobs = jobs[jobs["event"] == "schedule"] if len(jobs) else jobs
    runs_done = runs[runs["status"] == "completed"] if len(runs) else runs
    sched_runs = runs_done[runs_done["event"] == "schedule"] if len(runs_done) else runs_done
    s["prevalence"] = {
        "repos_with_workflows": int(n_wf),
        "cache_any_job_pct": pct(int(with_wf["uses_cache_any_job"].sum()), n_wf),
        "repos_with_install_steps": int((with_wf["jobs_with_install_steps"] > 0).sum()),
        "install_repos_with_cache_pct": pct(
            int(
                (
                    (with_wf["jobs_with_install_steps"] > 0)
                    & (with_wf["install_jobs_with_cache"] > 0)
                ).sum()
            ),
            int((with_wf["jobs_with_install_steps"] > 0).sum()),
        ),
        "timeout_any_job_pct": pct(int(with_wf["timeout_any_job"].sum()), n_wf),
        "timeout_all_jobs_pct": pct(int(with_wf["timeout_all_jobs"].sum()), n_wf),
        "pr_workflow_repos": int((with_wf["pr_workflows"] > 0).sum()),
        "concurrency_cancel_pct_of_pr_repos": pct(
            int(with_wf["concurrency_cancel_any_pr_workflow"].sum()),
            int((with_wf["pr_workflows"] > 0).sum()),
        ),
        "scheduled_workflow_repos_pct": pct(int((with_wf["scheduled_workflows"] > 0).sum()), n_wf),
        "workflow_files": len(wfs),
        "workflow_parse_errors": int(wfs["parse_error"].notna().sum()) if len(wfs) else 0,
        "workflows_with_path_filter_pct": pct(int(wfs["has_path_filter"].sum()), len(wfs))
        if len(wfs)
        else 0.0,
        "icse2024_baseline": ICSE,
    }
    s["runs"] = {
        "listed": len(runs),
        "completed": len(runs_done),
        "failed_pct_of_completed": pct(
            int(runs_done["conclusion"].isin(["failure", "timed_out"]).sum()), len(runs_done)
        )
        if len(runs_done)
        else 0.0,
        "scheduled_pct_of_completed": pct(len(sched_runs), len(runs_done))
        if len(runs_done)
        else 0.0,
        "scheduled_failed_pct": pct(
            int(sched_runs["conclusion"].isin(["failure", "timed_out"]).sum()), len(sched_runs)
        )
        if len(sched_runs)
        else 0.0,
        "reruns_pct_of_completed": pct(int((runs_done["run_attempt"] >= 2).sum()), len(runs_done))
        if len(runs_done)
        else 0.0,
        "by_event_pct": {
            str(k): pct(int(v), len(runs_done))
            for k, v in runs_done["event"].value_counts().head(8).items()
        }
        if len(runs_done)
        else {},
        "total_in_window_90d_sum": int(with_wf["runs_total_in_window_90d"].sum()),
        "repos_truncated_at_200_runs": int((with_wf["runs_total_in_window_90d"] > 200).sum()),
    }
    s["minutes"] = {
        "jobs_sampled": len(jobs),
        "runs_with_job_data": int(with_wf["runs_with_job_data"].sum()),
        "billed_minutes": round(billed),
        "raw_minutes": round(raw_min),
        "rounding_waste_minutes": round(waste_min),
        "rounding_waste_pct_of_billed": pct(waste_min, billed),
        "rounding_waste_pct_per_repo_median": round(statistics.median(per_repo_waste), 1)
        if per_repo_waste
        else 0.0,
        "rounding_waste_pct_per_repo_p90": round(q(per_repo_waste, 90), 1),
        "jobs_under_1_minute_pct": pct(int((jobs["raw_seconds"] < 60).sum()), len(jobs))
        if len(jobs)
        else 0.0,
        "jobs_under_2_minutes_pct": pct(int((jobs["raw_seconds"] < 120).sum()), len(jobs))
        if len(jobs)
        else 0.0,
        "failed_jobs_pct": pct(len(failed_jobs), len(jobs)) if len(jobs) else 0.0,
        "failed_jobs_minutes_pct": pct(float(failed_jobs["billed_minutes"].sum()), billed)
        if len(jobs)
        else 0.0,
        "scheduled_jobs_minutes_pct": pct(float(sched_jobs["billed_minutes"].sum()), billed)
        if len(jobs)
        else 0.0,
        "rerun_attempt_minutes_pct": pct(
            float(jobs[jobs["run_attempt"] >= 2]["billed_minutes"].sum()), billed
        )
        if len(jobs)
        else 0.0,
        "hosted_priced_minutes_pct": pct(
            float(jobs_priced[jobs_priced["sku"].notna()]["billed_minutes"].sum()), billed
        )
        if len(jobs)
        else 0.0,
        "hosted_unpriced_minutes_pct": pct(
            float(jobs_priced[jobs_priced["sku"].isna()]["billed_minutes"].sum()), billed
        )
        if len(jobs)
        else 0.0,
        "self_hosted_or_custom_minutes_pct": pct(
            float(jobs[jobs["runner_kind"] != "hosted"]["billed_minutes"].sum()), billed
        )
        if len(jobs)
        else 0.0,
        "by_sku_minutes": {
            str(k): int(v)
            for k, v in jobs.groupby(jobs["sku"].fillna("unpriced"))["billed_minutes"]
            .sum()
            .sort_values(ascending=False)
            .items()
        }
        if len(jobs)
        else {},
        "queue_minutes_pct_of_exec": pct(
            float(jobs["queue_seconds"].sum()), float(jobs["raw_seconds"].sum())
        )
        if len(jobs)
        else 0.0,
    }
    cost26 = float(jobs["cost_2026"].sum()) if len(jobs) else 0.0
    cost25 = float(jobs["cost_2025"].sum()) if len(jobs) else 0.0
    est90 = with_wf["est_list_price_2026_90d"]
    sh_min = (
        float(jobs[jobs["runner_kind"] != "hosted"]["billed_minutes"].sum()) if len(jobs) else 0.0
    )
    s["cost"] = {
        "sampled_jobs_list_price_2026": round(cost26, 2),
        "sampled_jobs_list_price_2025": round(cost25, 2),
        "list_price_change_pct_2026_vs_2025": round(100 * (cost26 - cost25) / cost25, 1)
        if cost25
        else 0.0,
        "repos_with_lower_2026_list_price_pct": pct(
            int((with_wf["list_price_2026_sampled"] < with_wf["list_price_2025_sampled"]).sum()),
            int((with_wf["list_price_2025_sampled"] > 0).sum()),
        ),
        "repos_with_equal_2026_list_price_pct": pct(
            int(
                (
                    (with_wf["list_price_2026_sampled"] == with_wf["list_price_2025_sampled"])
                    & (with_wf["list_price_2025_sampled"] > 0)
                ).sum()
            ),
            int((with_wf["list_price_2025_sampled"] > 0).sum()),
        ),
        "est_90d_list_price_2026_per_repo_median": round(float(est90.median()), 2)
        if len(est90)
        else 0.0,
        "est_90d_list_price_2026_per_repo_p90": round(float(est90.quantile(0.9)), 2)
        if len(est90)
        else 0.0,
        "est_90d_list_price_2026_per_repo_max": round(float(est90.max()), 2) if len(est90) else 0.0,
        "est_90d_list_price_2026_sum": round(float(est90.sum()), 2) if len(est90) else 0.0,
        "announced_self_hosted_charge_on_sampled_minutes": round(sh_min * 0.002, 2),
        "announced_self_hosted_charge_pct_of_2026_list_price": pct(sh_min * 0.002, cost26),
    }
    measured = findings[findings["kind"] == "measured"] if len(findings) else findings
    rules_out: dict[str, Any] = {}
    all_rules = sorted({*[r.id for r in STATIC_RULES], *[r.id for r in HISTORY_RULES]})
    for rule in all_rules:
        fr = findings[findings["rule"] == rule] if len(findings) else findings
        mr = measured[measured["rule"] == rule] if len(measured) else measured
        rec = float(mr["recoverable_minutes_est"].fillna(0).sum()) if len(mr) else 0.0
        rules_out[rule] = {
            "repos_flagged": int(fr["repo"].nunique()) if len(fr) else 0,
            "repos_flagged_pct": pct(int(fr["repo"].nunique()) if len(fr) else 0, n_wf),
            "findings": len(fr),
            "measured_findings": len(mr),
            "recoverable_minutes_est_sum": round(rec),
            "recoverable_minutes_est_pct_of_sampled_billed": pct(rec, billed),
            "recoverable_cost_2026_est_sum": round(
                float(mr["recoverable_cost_2026_est"].fillna(0).sum()), 2
            )
            if len(mr)
            else 0.0,
        }
    s["rules"] = rules_out
    s["h001_dead_cron_repos"] = (
        int(findings[findings["rule"] == "H001"]["repo"].nunique()) if len(findings) else 0
    )
    s["scheduled_repos"] = int((with_wf["scheduled_workflows"] > 0).sum())
    return s


def render_report(s: dict[str, Any]) -> str:
    smp = s["sample"]
    lines = [
        "# REPORT.md — GitHub Actions waste across a public corpus",
        "",
        f"Generated by `scripts/build_report.py` (ciburn {s['ciburn_version']}) on {s['generated_at']}. "
        "Every number below is computed from the files in `dataset/`; re-run the script to regenerate this file byte for byte. "
        "Nothing here was typed in by hand.",
        "",
        "## Sample construction",
        "",
        f"- Selection date: {smp['selected_at']}. Method: for each of {len(smp['languages'])} languages "
        f"({', '.join(smp['languages'])}) and {len(smp['star_buckets'])} star buckets "
        f"({', '.join(f'{a}-{b}' if b < 1_000_000 else f'{a}+' for a, b in smp['star_buckets'])}), the top {smp['per_cell']} "
        f"repositories by stars from the GitHub search API with `pushed:>={smp['pushed_since']} archived:false fork:false`. "
        "The exact query strings are in `dataset/sample.json`.",
        f"- Selected {smp['selected']} repositories; scanned {smp['scanned']}; {smp['with_workflows']} have at least one workflow file, "
        f"{smp['without_workflows']} have none"
        + (
            f"; {smp['not_scanned_or_failed']} were not scanned (scan interrupted or API error)."
            if smp["not_scanned_or_failed"]
            else "."
        ),
        f"- Window: the {smp['window_days']} days before each repository's scan. Per repository: all workflow files, the file tree, "
        "up to 200 runs (the API's `total_count` for the window is recorded separately), and the jobs of up to 8 runs "
        "(newest completed run of each workflow first, then newest overall).",
        "- Public repositories only, REST API only, aggregate results only. Job-level minutes are therefore a **sample**, "
        "not the full window; percentages are of the sample, and 90-day totals are labelled estimates.",
        "",
    ]
    if "prevalence" not in s:
        lines += ["_No scanned repositories with workflows yet._", ""]
        return "\n".join(lines)
    p, r, m, c, ru = s["prevalence"], s["runs"], s["minutes"], s["cost"], s["rules"]
    b = p["icse2024_baseline"]
    lines += [
        "## 1. How prevalent is each optimisation, versus the 2024 academic baseline?",
        "",
        f"Baseline: Bouzenia & Pradel, ICSE 2024 ({b['source']}), paid-tier repositories, 30 months of history.",
        "",
        "| measure | this corpus | ICSE 2024 |",
        "|---|---:|---:|",
        f"| repositories using a dependency cache in any job | {p['cache_any_job_pct']} % of {p['repos_with_workflows']} | {b['caching_paid_tier_pct']} % |",
        f"| … among repositories with an install step | {p['install_repos_with_cache_pct']} % of {p['repos_with_install_steps']} | — |",
        f"| repositories setting `timeout-minutes` on any job | {p['timeout_any_job_pct']} % | {b['custom_timeout_paid_tier_pct']} % |",
        f"| repositories setting `timeout-minutes` on every job | {p['timeout_all_jobs_pct']} % | — |",
        f"| PR workflows with `concurrency` + `cancel-in-progress` (repos) | {p['concurrency_cancel_pct_of_pr_repos']} % of {p['pr_workflow_repos']} | — |",
        f"| workflow files with a `paths`/`paths-ignore` filter | {p['workflows_with_path_filter_pct']} % of {p['workflow_files']} | — |",
        f"| repositories with a scheduled workflow | {p['scheduled_workflow_repos_pct']} % | — |",
        f"| completed runs that failed | {r['failed_pct_of_completed']} % of {r['completed']} | {b['failed_runs_pct']} % |",
        f"| sampled billed minutes in failed jobs | {m['failed_jobs_minutes_pct']} % | {b['failed_vm_time_pct']} % (VM time) |",
        f"| scheduled runs, share of completed runs | {r['scheduled_pct_of_completed']} % | {b['scheduled_runs_pct']} % |",
        f"| scheduled jobs, share of sampled minutes | {m['scheduled_jobs_minutes_pct']} % | {b['scheduled_vm_time_pct']} % |",
        f"| scheduled runs that failed | {r['scheduled_failed_pct']} % | {b['scheduled_failure_pct']} % |",
        f"| repositories with a dead cron (H001, ≥3 consecutive scheduled failures) | {s['h001_dead_cron_repos']} of {s['scheduled_repos']} with schedules | — |",
        "",
        "The corpus is public repositories priced at private list rates; the baseline is paid-tier (private) repositories. "
        "Comparable in kind, not in population.",
        "",
        "## 2. How much sampled CI time is attributable to each waste pattern?",
        "",
        f"Sampled: {m['jobs_sampled']:,} jobs from {m['runs_with_job_data']:,} runs, {m['billed_minutes']:,} billed minutes "
        f"({m['raw_minutes']:,} minutes of actual execution). Recoverable minutes are ciburn's per-finding estimates, "
        "summed; methods are in `dataset/findings.csv`; estimates for different rules can overlap.",
        "",
        "| rule | repos flagged | measured findings | recoverable minutes (est.) | % of sampled billed minutes |",
        "|---|---:|---:|---:|---:|",
    ]
    for rule, v in ru.items():
        lines.append(
            f"| {rule} | {v['repos_flagged']} ({v['repos_flagged_pct']} %) | {v['measured_findings']} | "
            f"{v['recoverable_minutes_est_sum']:,} | {v['recoverable_minutes_est_pct_of_sampled_billed']} % |"
        )
    lines += [
        "",
        f"Other measured shares of sampled billed minutes: failed jobs {m['failed_jobs_minutes_pct']} %, "
        f"run attempts ≥ 2 (re-runs) {m['rerun_attempt_minutes_pct']} %, scheduled jobs {m['scheduled_jobs_minutes_pct']} %. "
        f"Queue time was {m['queue_minutes_pct_of_exec']} % of execution time (not billed).",
        "",
        "## 3. How does per-job minute rounding change the picture?",
        "",
        f"GitHub rounds every job up to a whole minute. In the sample, {m['rounding_waste_minutes']:,} of {m['billed_minutes']:,} "
        f"billed minutes ({m['rounding_waste_pct_of_billed']} %) are rounding: time billed but not executed. "
        f"Per repository the median share is {m['rounding_waste_pct_per_repo_median']} % and the 90th percentile "
        f"{m['rounding_waste_pct_per_repo_p90']} %. {m['jobs_under_1_minute_pct']} % of sampled jobs ran under one minute and "
        f"{m['jobs_under_2_minutes_pct']} % under two; every one of them bills a whole minute per leg.",
        "",
        "## 4. What would the sample cost at private-repository rates?",
        "",
        f"At 2026 list prices the sampled jobs would cost ${c['sampled_jobs_list_price_2026']:,.2f}; at the 2025 rates "
        f"${c['sampled_jobs_list_price_2025']:,.2f} ({c['list_price_change_pct_2026_vs_2025']:+.1f} %). "
        f"Extrapolating each repository's sampled minutes-per-run to its 90-day run count gives a median of "
        f"${c['est_90d_list_price_2026_per_repo_median']:,.2f} per repository per 90 days, p90 "
        f"${c['est_90d_list_price_2026_per_repo_p90']:,.2f}, max ${c['est_90d_list_price_2026_per_repo_max']:,.2f}, "
        f"sum ${c['est_90d_list_price_2026_sum']:,.2f} (estimate: assumes the sampled runs are typical of the window).",
        "",
        f"Runner mix of sampled minutes: {m['hosted_priced_minutes_pct']} % on GitHub-hosted runners with a known SKU, "
        f"{m['hosted_unpriced_minutes_pct']} % on GitHub-hosted runners with an org-defined label (unpriced), "
        f"{m['self_hosted_or_custom_minutes_pct']} % on self-hosted or third-party runners ($0 under the models in effect).",
        "",
        '## 5. GitHub\'s "96 % of customers will see no change" statement',
        "",
        "GitHub said (2025-12-16) that 96 % of customers would see no bill change from the 2026 pricing update. "
        "This corpus cannot test that claim: it sees no bills, no included-minute drawdown, and no private usage. "
        "What it can say, at list price and for the runners it can identify:",
        "",
        f"- {c['repos_with_lower_2026_list_price_pct']} % of repositories with priced minutes have a lower 2026 list price than 2025 "
        f"for the same sampled minutes; {c['repos_with_equal_2026_list_price_pct']} % are unchanged (only arm64 2-core Linux kept its price).",
        f"- The postponed $0.002/minute self-hosted charge would have added ${c['announced_self_hosted_charge_on_sampled_minutes']:,.2f} "
        f"to the sampled minutes on self-hosted/third-party runners, {c['announced_self_hosted_charge_pct_of_2026_list_price']} % "
        "of the sample's 2026 hosted list price (public repositories would have stayed free).",
        "",
        "## Limitations and what this data cannot support",
        "",
        "- Job-level data is a sample of at most 8 runs per repository; run listings are capped at 200 per repository "
        f"({r['repos_truncated_at_200_runs']} repositories exceeded it). Absolute minute totals are therefore not window totals.",
        "- The sample is the most-starred repositories per language/bucket that were pushed recently, not a random sample of GitHub.",
        "- Public repositories: their standard-runner minutes cost nothing on GitHub. Every dollar figure is a list-price equivalent.",
        "- Larger Linux/Windows runners use org-defined labels and are unpriced here; macOS large/xlarge labels are priced.",
        "- Recoverable-minute estimates are per-rule scenarios with stated methods; they overlap and are upper bounds where noted.",
        "- Nothing here measures anyone's bill, included-minute consumption, or the effect of the postponed self-hosted charge on real accounts.",
        "",
        "## Reproduce",
        "",
        "```bash",
        "GITHUB_TOKEN=... uv run python scripts/corpus_scan.py --workdir corpus --per-cell 50",
        "uv run python scripts/build_report.py --workdir corpus --out dataset --report REPORT.md",
        "```",
        "",
        "Schema: `dataset/SCHEMA.md`. Headline numbers: `dataset/summary.json`.",
        "",
    ]
    return "\n".join(lines)


def write_schema(out: Path) -> None:
    text = """# dataset/ schema

All files are written by `scripts/build_report.py`; CSV and Parquet carry the same rows.

## repos
One row per selected repository. `scan_status` is `done`, `gone`, `error` or `not_scanned`; columns after it are
present only for `done`. `*_sampled` columns count the jobs of the up-to-8 sampled runs; `runs_total_in_window_90d`
is the API `total_count` of runs created in the 90-day window; `est_*_90d` extrapolate sampled minutes-per-run to
that count (estimate). `uses_cache_any_job`, `timeout_*`, `concurrency_cancel_any_pr_workflow` are static facts.
`list_price_*` are USD at the named model's list rates.

## workflows
One row per workflow file: triggers (`events`), `crons`, job counts, timeout/cache/matrix facts, `concurrency_cancel`,
`has_path_filter`, and `runs_listed` (runs of that workflow among the listed ones).

## runs
One row per listed run (<= 200 per repository): event, status, conclusion, `run_attempt`, `wall_seconds`
(`updated_at - run_started_at`), `jobs_fetched`.

## jobs
One row per sampled job: `raw_seconds` (`completed_at - started_at`), `billed_minutes` (rounded up per job),
`rounding_waste_seconds`, `queue_seconds` (`started_at - created_at`), `runner_kind` (`hosted` or
`self_hosted_or_custom`), `sku` (null when unpriced), `labels`, `cost_2026` / `cost_2025` (USD list price).

## findings
One row per finding per repository from the full ciburn pipeline (static rules joined to history, plus history
rules): `kind` (`measured` or `advisory`), severity, confidence, observed and recoverable minutes/cost, and the
`estimate_method` string.

## summary.json
Every number quoted in REPORT.md, keyed by section.

## sample.json
The selection record: date, queries, per-cell size, and the ordered list of selected repositories.
"""
    (out / "SCHEMA.md").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
