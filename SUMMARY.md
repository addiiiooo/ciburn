# SUMMARY.md

## What was built

`ciburn` 0.1.0, a Python 3.11+ CLI (`click` + `rich` + `httpx` + `pyyaml`)
that answers "what does this repository's GitHub Actions CI cost and where is
it wasted" from the repository's own run history.

- **Static analyzer** (`src/ciburn/rules/static_*.py`): W000–W012 over
  `.github/workflows/*.yml`, no network. Each rule has a stable id, severity,
  confidence, evidence rows and a machine-readable remediation.
- **History layer** (`src/ciburn/history/`): rate-limit-aware REST client,
  SQLite cache (repos, workflows, workflow files, runs, jobs, steps, commits,
  timings), incremental and resumable ingestion, typed/priced `HistoryView`.
- **History rules** (`src/ciburn/rules/history_rules.py`): H001–H009, measured
  by construction.
- **Pricing engine** (`src/ciburn/pricing.py` + `data/pricing.yaml`): 2026
  rates in effect, 2025 rates, and the announced-then-postponed self-hosted
  scenario; GitHub's per-job round-up; label→SKU mapping; included-minutes
  offset under a labelled assumption. No price in code.
- **The join** (`src/ciburn/join.py`): attaches observed minutes, list-price
  cost, recoverable estimate, method and confidence to every static finding
  that has matching history; anything else stays *advisory*.
- **Interface**: `ciburn audit` (terminal/md/json/html, `--fail-on`,
  `--ignore`, `# ciburn-ignore:` comments), `ciburn price --compare`,
  `ciburn fix` (verified unified diff, never commits), `ciburn rules`,
  `ciburn models`; `action.yml` composite action that posts a PR comment.
- **Corpus study**: `scripts/corpus_scan.py` (stratified, checkpointed,
  resumable), `scripts/build_report.py` (dataset CSV+Parquet+SCHEMA.md+
  summary.json and REPORT.md generated from data), `scripts/build_launch.py`
  (launch posts generated from `summary.json`).

## What was verified, and how

- `ruff check`, `ruff format --check`, `mypy --strict`: clean.
- `pytest`: 203 tests pass, 1 live test skipped by default. Coverage of
  `ciburn.rules` + `ciburn.pricing` + `ciburn.join` is about 97 % (CI floor 90 %).
  Golden fixtures: a positive and a negative workflow per static rule
  (`tests/fixtures/workflows/`, `tests/fixtures/expected/`). Property tests on
  the pricing engine (non-negative, monotonic, exact at 0/1/59/60/61 s).
  API tests replay recorded fixtures through `httpx.MockTransport`; the unit
  suite makes no network calls.
- Clean room: `uv build` → wheel installed into an empty Python 3.11 venv →
  audits of `pallets/flask`, `astral-sh/ruff`, `BurntSushi/ripgrep` ran with
  exit 0 and no traceback; totals reconcile (billed minutes and cost by SKU
  and by workflow equal the totals; every finding's observed ≤ total and
  recoverable ≤ observed). Real outputs: `docs/example-*.txt`, `docs/demo.svg`.
- Pricing facts verified against docs.github.com, github.blog and the
  `github/docs` git history with URLs and timestamps (`RESEARCH.md`).
- Three claims in the original brief were found false and corrected
  (`RESEARCH.md`): hosted prices fell rather than rose; the self-hosted charge
  is postponed, not in effect; "13 % of scheduled workflows fail
  consecutively" is actually the scheduled-run failure rate.

## What was cut or left unfinished, and why

- **Corpus scan not complete at hand-off.** The scan (1,993 selected
  repositories, ~14 API requests each at 5,000/hour) was still running;
  `dataset/` and `REPORT.md` were generated from the repositories scanned so
  far and say so ("scanned N of 1993"). Regenerate when the scan ends
  (`RESUME.md`).
- **Not pushed, no CI run observed.** The brief asked for the repository
  committed locally and remote-ready. The CI matrix (ubuntu/macos/windows ×
  3.11/3.12/3.13) has not executed anywhere yet; "all green" is unverified.
- **No PyPI release**; install from the checkout.
- **W011 (artifacts)** stays advisory: the artifacts API was not ingested.
- **Larger Linux/Windows runners** cannot be identified from job labels; they
  are reported as unpriced minutes unless mapped with `--label-sku`.
- **`ciburn fix`** supports the mechanical patches (concurrency, timeouts,
  branches/paths filters, fail-fast, runs-on, needs); everything else is
  advice.

## Known limitations

- Minutes are computed from job timestamps because GitHub's usage endpoints
  return zero for public repositories and are closing down; for private
  repositories the `/timing` endpoint is still fetched but only stored.
- Public repositories are priced at private list rates, always labelled.
- Included-minute drawdown for non-Linux runners is an assumption (D005).
- Recoverable estimates per rule overlap; the summed figure is capped at the
  observed total and labelled non-additive.
- Job data for very large repositories is sampled when unauthenticated or
  when `--max-job-runs` is set; reports state the coverage.
- `tests/test_pricing_freshness.py` fails on purpose 90 days after the
  pricing fetch date (2026-12-10).

## Check it yourself (first commands to run)

```bash
uv sync --group dev
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest -q --cov=ciburn.rules --cov=ciburn.pricing --cov=ciburn.join --cov-fail-under=90
uv run ciburn audit --path . --no-history           # static only, offline
GITHUB_TOKEN=... uv run ciburn audit --repo pallets/flask --days 30
GITHUB_TOKEN=... uv run ciburn price --repo pallets/flask --days 30 --compare
uv run ciburn fix --path . --no-history             # prints a diff, changes nothing
uv build && uv venv /tmp/clean && uv pip install --python /tmp/clean/bin/python dist/*.whl && /tmp/clean/bin/ciburn --version
uv run python scripts/build_report.py --workdir corpus --out dataset --report REPORT.md   # needs corpus/ from the scan
CIBURN_LIVE=1 uv run pytest tests/test_live.py       # one real network test
```
