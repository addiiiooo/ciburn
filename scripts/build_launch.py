"""Generate launch/ posts from dataset/summary.json so every number is traceable.

Usage: python scripts/build_launch.py [--summary dataset/summary.json] [--out launch]
Re-run after every `build_report.py`; never edit the numbers by hand.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: expected a JSON object")
    return data


def facts(s: dict[str, Any]) -> dict[str, Any]:
    smp, p, m, c, r, ru = (
        s["sample"],
        s["prevalence"],
        s["minutes"],
        s["cost"],
        s["runs"],
        s["rules"],
    )
    return {
        "n": smp["with_workflows"],
        "scanned": smp["scanned"],
        "selected": smp["selected"],
        "partial": smp["scanned"] < smp["selected"],
        "jobs": m["jobs_sampled"],
        "billed": m["billed_minutes"],
        "waste_pct": m["rounding_waste_pct_of_billed"],
        "waste_median": m["rounding_waste_pct_per_repo_median"],
        "waste_p90": m["rounding_waste_pct_per_repo_p90"],
        "under1": m["jobs_under_1_minute_pct"],
        "cache_pct": p["cache_any_job_pct"],
        "timeout_any": p["timeout_any_job_pct"],
        "timeout_all": p["timeout_all_jobs_pct"],
        "cancel_pct": p["concurrency_cancel_pct_of_pr_repos"],
        "pr_repos": p["pr_workflow_repos"],
        "dead_cron": s["h001_dead_cron_repos"],
        "sched_repos": s["scheduled_repos"],
        "h002_repos": ru["H002"]["repos_flagged"],
        "h002_pct": ru["H002"]["recoverable_minutes_est_pct_of_sampled_billed"],
        "rerun_pct": m["rerun_attempt_minutes_pct"],
        "failed_min_pct": m["failed_jobs_minutes_pct"],
        "failed_runs_pct": r["failed_pct_of_completed"],
        "price_delta": c["list_price_change_pct_2026_vs_2025"],
        "median90": c["est_90d_list_price_2026_per_repo_median"],
        "p9090": c["est_90d_list_price_2026_per_repo_p90"],
        "icse_cache": p["icse2024_baseline"]["caching_paid_tier_pct"],
        "icse_timeout": p["icse2024_baseline"]["custom_timeout_paid_tier_pct"],
        "date": s["generated_at"][:10],
    }


def hn(f: dict[str, Any]) -> str:
    note = (
        f"\n\n_Sample note: {f['scanned']} of {f['selected']} selected repositories scanned when this was generated; "
        "regenerate from dataset/summary.json before posting._"
        if f["partial"]
        else ""
    )
    return f"""# Show HN post

**Title:** Show HN: ciburn - what your GitHub Actions CI costs, from your own run history

Across {f["n"]} public repositories ({f["jobs"]:,} sampled jobs), {f["waste_pct"]} % of billed GitHub Actions minutes were never executed. GitHub rounds every job up to a whole minute, and {f["under1"]} % of jobs finish in under one. For the median repository that rounding is {f["waste_median"]} % of its bill; at the 90th percentile, {f["waste_p90"]} %.

ciburn is a Python CLI that reads .github/workflows, joins each finding to the repository's actual runs, jobs and steps, and prices them under GitHub's 2026 rates. Every number is an observed measurement or an estimate that states its method. No LLM, no service, no telemetry.

Also in the corpus: {f["cache_pct"]} % of repositories cache dependencies (ICSE 2024: {f["icse_cache"]} % of paid-tier repos); {f["timeout_all"]} % set timeout-minutes on every job; {f["dead_cron"]} of {f["sched_repos"]} repositories with schedules have a cron that failed three or more times in a row.

Dataset, method and limitations are in the repo. Figures are list-price equivalents; public repos pay nothing.

https://github.com/addiiiooo/ciburn{note}
"""


def reddit(f: dict[str, Any]) -> str:
    return f"""# r/devops post

**Title:** I measured GitHub Actions waste across {f["n"]} public repos and built a CLI that does it for yours

The interesting part is not "add a cache". It is that GitHub bills each job rounded up to a whole minute, and {f["under1"]} % of sampled jobs finish in under one. In the corpus {f["waste_pct"]} % of all billed minutes are rounding; the median repository loses {f["waste_median"]} % of its bill to it, the 90th percentile {f["waste_p90"]} %. Wide matrices of 40-second jobs are the worst case.

Other numbers from the same dataset ({f["jobs"]:,} sampled jobs, generated {f["date"]}):

- {f["cache_pct"]} % of repositories use a dependency cache in any job (ICSE 2024: {f["icse_cache"]} % of paid-tier repos)
- {f["timeout_any"]} % set `timeout-minutes` anywhere; {f["timeout_all"]} % on every job (default is 360 minutes)
- {f["cancel_pct"]} % of the {f["pr_repos"]} repositories with PR workflows cancel superseded runs
- {f["dead_cron"]} of {f["sched_repos"]} repositories with schedules have a cron that failed 3+ times in a row
- re-run attempts are {f["rerun_pct"]} % of sampled minutes; failed jobs {f["failed_min_pct"]} %
- the 2026 repricing cut the list price of the same minutes by {abs(f["price_delta"])} %

The tool (`ciburn audit`) reads your workflows, pulls your run/job/step history into SQLite, and attaches observed minutes and money to each finding, with the estimate method printed next to it. Advisory findings (config only, no history) are kept in a separate table so an estimate never looks like a measurement. `ciburn price --compare` prices the same history under 2025 and 2026 rates. `ciburn fix` prints a diff.

Limits: job data is sampled (up to 8 runs per repo); public repos are priced at private list rates because they pay nothing; larger Linux/Windows runners use org labels and are unpriced.

Repo, dataset and REPORT.md: https://github.com/addiiiooo/ciburn
"""


def linkedin(f: dict[str, Any]) -> str:
    return f"""# LinkedIn post

I spent a session building ciburn, an open-source CLI that tells a team what its GitHub Actions CI costs and where the minutes go to waste, using the repository's own run history rather than generic advice.

The finding that surprised me most, from a scan of {f["n"]} public repositories: {f["waste_pct"]} % of billed minutes were never executed. GitHub rounds every job up to a whole minute, and {f["under1"]} % of jobs finish in under one. Matrix builds of short jobs pay for this many times over.

Also in the data: {f["cache_pct"]} % of repositories cache dependencies, {f["timeout_all"]} % set a timeout on every job, and {f["dead_cron"]} of {f["sched_repos"]} repositories with a schedule have a cron that keeps failing run after run.

ciburn prices history under GitHub's 2026 rates (down up to 39 % since January; the self-hosted charge was postponed), separates measured findings from advisory ones, and states the method behind every estimate. MIT licensed, no LLM, no service.

https://github.com/addiiiooo/ciburn
"""


def tweets(f: dict[str, Any]) -> str:
    return f"""# Tweet thread

1/ GitHub bills every Actions job rounded up to a whole minute. Across {f["n"]} public repos ({f["jobs"]:,} sampled jobs), {f["waste_pct"]} % of billed minutes were never executed. Median repo: {f["waste_median"]} % of its bill is rounding. I built a CLI that shows you yours.

2/ ciburn reads .github/workflows, pulls your run/job/step history into SQLite, and attaches observed minutes + money to every finding, with the estimate method printed next to it. 2026 rates. No LLM, no service. `uv tool install .` then `ciburn audit`.

3/ Same dataset: {f["cache_pct"]} % of repos cache deps, {f["timeout_all"]} % set a timeout on every job, {f["dead_cron"]} of {f["sched_repos"]} with schedules have a cron failing 3+ times in a row. Dataset + method: https://github.com/addiiiooo/ciburn
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", type=Path, default=Path("dataset/summary.json"))
    ap.add_argument("--out", type=Path, default=Path("launch"))
    args = ap.parse_args()
    f = facts(load(args.summary))
    args.out.mkdir(parents=True, exist_ok=True)
    header = (
        f"<!-- generated by scripts/build_launch.py from {args.summary} ({f['date']}); "
        f"{f['scanned']}/{f['selected']} repositories scanned. Do not edit numbers by hand. -->\n\n"
    )
    for name, fn in (
        ("show-hn.md", hn),
        ("reddit-devops.md", reddit),
        ("linkedin.md", linkedin),
        ("tweets.md", tweets),
    ):
        (args.out / name).write_text(header + fn(f), encoding="utf-8")
        print(f"wrote {args.out / name}")


if __name__ == "__main__":
    main()
