# dataset/ schema

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
