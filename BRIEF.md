# MISSION

You are building and shipping a complete open-source project, start to finish, in one autonomous session. No human will answer questions during this run. Assume every question you might ask goes unanswered — decide, log the decision, continue.

**Deliverable:** a production-quality Python CLI (working name `ciburn`) that tells any team exactly what their GitHub Actions CI is costing them and where the money is going to waste — plus a public corpus study that proves the problem at scale.

Working directory: the current directory. Create the repository here. Intended remote: `github.com/addiiiooo/<name>`. Do not push; leave it committed locally with a remote-ready state and tell me the push command at the end.

---

# WHY THIS EXISTS — the thesis you are proving

1. **CI waste is measured, not guessed.** An ICSE 2024 study of 1.3M workflow runs across 952 repositories found: only 32.9% of paid-tier repos use caching; only 14% set custom `timeout-minutes` against a 360-minute default; failed runs consume 30.9% of paid-tier VM time while being 17.4% of runs; 13% of scheduled workflows fail consecutively, one repository logging roughly 12,000 consecutive failing scheduled runs.
2. **The price of that waste changed in 2026.** GitHub repriced hosted runners on 2026-01-01 and introduced a $0.002/minute Actions platform charge; from 2026-03-01 self-hosted runner minutes are billed for the first time. Every team's mental model of CI cost is now out of date.
3. **Existing tools do not close this.** `actionlint` checks syntax. `zizmor` checks security. Neither joins workflow configuration to a repository's actual run history and actual money. The nearest cost-focused OSS tool has a single commit and no users.

**The differentiator you must not lose:** every finding is attributed to real observed minutes and real money from that repository's own run history. Generic advice is worthless and already exists. "Add a cache" is worthless. This is the product:

> `test (ubuntu-latest, 3.11)` burned 4,210 billable minutes across 812 runs in the last 90 days. 71% of that is dependency installation. A cache key on `~/.cache/uv` recovers an estimated 2,900 min/quarter ≈ $23/month at your current rate. Confidence: high (measured, 812 runs).

---

# PHASE 0 — VERIFY THE WORLD BEFORE YOU BUILD

Your training data is stale and the facts above came from a research pass you did not run. Verify before building on them.

**0.1 Prior-art check.** Search GitHub, PyPI and npm for tools that join Actions run history to cost. If a mature, actively maintained equivalent exists (>1,000 stars, commits within 90 days) that already does history-attributed cost analysis, do **not** build a duplicate: write `PIVOT.md` naming the incumbent, narrow this project to the gap it leaves — most likely the 2026 pricing model, self-hosted minute accounting, and the public corpus study — and continue with the narrowed scope. Never abandon the session.

**0.2 Pin the pricing facts.** Fetch GitHub's current Actions billing documentation and pricing pages from primary sources (`docs.github.com`, `github.com/pricing`, the GitHub changelog). Extract, each with a source URL and fetch timestamp: per-minute rates by runner OS and size; the platform charge and its effective date; the self-hosted billing change and its effective date; included minutes per plan; OS cost multipliers; and the **rounding rule** — GitHub rounds each job up to the nearest whole minute. Verify that rule and treat it as load-bearing: it is what makes wide matrices of very short jobs disproportionately expensive, and it is an insight most teams have never costed.

Write this to `src/<pkg>/data/pricing.yaml`. Every value carries `source_url` and `fetched_at`. Never hardcode a price in code. Add a test that fails loudly if `pricing.yaml` is older than 90 days. If a number cannot be verified from a primary source, mark it `unverified: true`, exclude it from cost math, and say so in the report rather than guessing.

**0.3 Pin the API surface.** Confirm against `docs.github.com` which endpoints exist and what they return: list workflow runs; get workflow run usage (billable ms per OS); list jobs for a run (per-job and per-step timing, `runner_name`, `labels`, conclusion); list workflows; and the current billing/usage endpoints including any required `apiVersion`. Record real response shapes as test fixtures. Build against what you observe, not what you remember.

**0.4 Name check.** Confirm the package name is free on PyPI and the repository name free on GitHub before writing it into 200 files. Fallback candidates: `ciburn`, `runnerburn`, `ciwaste`, `actionspend`, `burnmeter`.

Write Phase 0 output to `RESEARCH.md` with every source URL. If Phase 0 contradicts the thesis above, follow Phase 0 and record the correction.

---

# WHAT TO BUILD

## Static analyzer — reads `.github/workflows/*.yml`, no network

Each rule has a stable ID, severity, confidence, and machine-readable remediation.

- `W001` PR-triggered workflow without `concurrency` + `cancel-in-progress` — superseded runs keep burning
- `W002` no dependency cache (`actions/cache`, or `setup-*` with `cache:`)
- `W003` job without `timeout-minutes` — exposure is the 360-minute default
- `W004` push/PR trigger without `paths` / `paths-ignore` where the repo has separable trees (`docs/`, `*.md`)
- `W005` redundant matrix legs, or a matrix whose per-leg runtime is short enough that per-job minute rounding dominates real work
- `W006` `actions/checkout` with `fetch-depth: 0` and no observable need
- `W007` runner label larger than observed utilisation justifies
- `W008` `schedule` cron firing far more often than the repository actually changes
- `W009` overlapping triggers producing duplicate runs for one commit (`push` + `pull_request` on the same branch)
- `W010` expensive jobs with no guard for draft PRs or docs-only changes
- `W011` artifact upload of large paths at default retention

Add rules you can justify. Every new rule needs a fixture and either a citation or a clear derivation.

## History analyzer — run/job/step history into a local SQLite cache

Resumable, incremental, never re-fetches what it already has.

- `H001` scheduled workflow failing consecutively k≥3 — dead cron burning money
- `H002` scheduled runs on a repository with no code change since the previous run
- `H003` jobs that have never failed and gate nothing — candidates for sampling or removal
- `H004` failure hotspots: jobs whose failed runs consume disproportionate minutes
- `H005` p95 runtime far above median — flaky or timeout tails
- `H006` re-run tax: minutes spent re-running the same commit
- `H007` jobs terminating at the default timeout
- `H008` queue time versus execution time
- `H009` job ordering: expensive jobs that could sit behind a cheap fast-fail gate

## Pricing engine

Converts observed minutes into money under the 2026 model: per-job rounding, OS multipliers, platform charge, self-hosted minutes, included-minutes offset. It must price the same history under more than one model, so the tool can answer "what did this cost before the change, and after".

## The join

Every finding carries: rule ID, affected workflow/job, observed minutes in the window, estimated recoverable minutes, estimated money, confidence, the evidence rows it derives from, and the concrete remediation. A finding with no observed evidence is emitted as `advisory` and is visually separated from measured findings. **Never present an estimate as a measurement.**

## Interface

- `<tool> audit [--repo owner/name | --path .] [--days 90] [--format terminal|md|json|html]`
- `<tool> price --repo owner/name --model 2026|2025 --compare`
- `<tool> fix [--rules W001,W003] [--dry-run]` — writes a patch; never commits, never pushes, never opens a PR on its own
- Works with no token against public repositories (document the rate limits); `GITHUB_TOKEN` for private repos and higher limits
- CI-suitable exit codes: 0 clean, 1 findings above threshold, 2 error
- A GitHub Action wrapper in `action.yml` that posts a PR comment
- **Zero LLM calls at runtime.** The shipped tool works offline and free, forever. Non-negotiable — it is why people will adopt it.

## The moat — the corpus study

Scan a large sample of public repositories (target 2,000–5,000, stratified by language and star count, selection method written down and reproducible). Respect the API: authenticated requests, descriptive User-Agent, exponential backoff on 403/429, checkpointed and resumable, never scrape HTML where an API exists, public repositories only, aggregate data only.

Produce:
- `dataset/` — committed results as parquet + CSV with a schema document
- `scripts/corpus_scan.py` and `scripts/build_report.py` — anyone can regenerate every number
- `REPORT.md` — findings, method, sample construction, limitations, and an explicit statement of what the data cannot support

Frame the report around checkable questions, not hype: how much CI time in the sample is attributable to each waste pattern; how prevalence compares to the 2024 academic baseline; what the sample would cost at private-repo rates; how per-job minute rounding changes the picture. GitHub states 96% of customers saw no bill change after the repricing — where your data can speak to that, say what it shows; where it cannot, say so plainly.

---

# HARD CONSTRAINTS — violating any of these fails the run

## Autonomy

- Never ask a question. Never wait. When ambiguous, choose the option that is easiest to verify, smallest in blast radius, and most reversible — then append the decision, the alternatives, and the reasoning to `DECISIONS.md`.
- When blocked: three genuinely different attempts, then write a `BLOCKERS.md` entry, implement the degraded path, and move on. Never stall.
- Never leave a stub that pretends to work. An unfinished feature raises `NotImplementedError`, is excluded from the CLI surface, and is listed under Roadmap — not Features.

## Honesty — this repository goes on a résumé; one fabricated number is worse than a missing feature

- Every number in the README, REPORT and docs must be regenerable by a committed script. If you cannot regenerate it, delete it.
- No invented benchmarks. No example output you did not actually produce. No fabricated testimonials, stars, or adoption claims.
- Every external claim carries a source URL.
- Cite prior art by name — `actionlint`, `zizmor`, the ICSE 2024 study, anything found in Phase 0. Claim novelty only for what is actually novel: the history-to-cost join, the 2026 pricing model, the corpus dataset.
- Savings figures are labelled estimates, with the method stated inline and a stated confidence.
- No claim or implication of GitHub endorsement.

## Testing

- `ruff`, `mypy --strict`, `pytest`. Coverage floor 90% on analyzer, pricing and join modules, enforced in CI.
- Golden-file tests: fixture workflows in `tests/fixtures/` paired with expected-findings JSON. Every rule gets at least one positive and one negative fixture.
- Property tests on the pricing engine: never negative, monotonic in minutes, rounding boundaries exact at 0s / 1s / 59s / 60s / 61s.
- API tests replay recorded fixtures. Unit tests make zero network calls. One live integration test, marked and skippable.
- **Never weaken, skip or delete a test to make the suite pass.** If a test is wrong, fix the test and explain why in the commit message.

## Clean-room verification — the step autonomous builds skip; do not skip it

- Build the wheel. Install it in a fresh virtual environment with nothing preinstalled.
- Run the CLI against three named, real public repositories of different shapes.
- Assert output is non-empty, internally consistent (minutes reconcile, money reconciles), and free of tracebacks.
- Paste the **real** output into the README. Not a mock-up.
- Dogfood: the repository's own CI runs the tool on itself and fails the build on regressions.

## Repository quality

- MIT licence, `pyproject.toml`, Python 3.11+, installable via `uv tool install` / `pipx` / `uvx`
- CI matrix: ubuntu + macos + windows × 3.11 / 3.12 / 3.13, all green before you finish
- `CONTRIBUTING.md`, issue and PR templates, `CHANGELOG.md`, semantic version `0.1.0`
- Conventional commits, one per completed unit of work. No single giant commit.
- Correct `.gitignore`; no secrets, tokens, `.env` files or cache databases committed. Scan the full diff for secrets before the final commit.
- Demo image: you cannot record a screencast, so render the real terminal output to SVG via `rich`'s `Console.save_svg()` from a committed script, and embed that. Never fake a screenshot.

## Scope freeze — non-goals for v1

Do not build these; list them in the README so the boundary reads as deliberate: no web dashboard or hosted service; no CI providers other than GitHub Actions; no runtime LLM; no auto-merge or auto-PR; no telemetry of any kind; no database server (SQLite only); no auth beyond a user-supplied token.

## Priority order if you run short

Protect in this order, cut from the bottom: (1) static + history analyzers correct and tested, (2) clean install works on a fresh machine, (3) README honest, clear and reproducible, (4) corpus study and dataset, (5) `fix`, (6) the GitHub Action wrapper.

---

# SEQUENCE

Commit at every gate. Append to `RUNLOG.md` at every gate: what was built, what was verified, what remains. `RUNLOG.md` and `DECISIONS.md` are how you survive your own context compaction — write to them as if the next phase will be executed by someone who has read nothing else.

1. Phase 0 research → `RESEARCH.md`, `pricing.yaml`, API fixtures, name confirmed
2. Skeleton, packaging, CI, test harness — green CI on an empty project before writing any feature
3. Static analyzer + every rule fixture
4. History ingestion, SQLite cache, rate-limit handling, resume
5. Pricing engine + property tests
6. The join, findings model, reporters (terminal / md / json / html)
7. `fix` + `action.yml`
8. **Scope freeze.** No new features past this line. Corpus scan → dataset → `REPORT.md`
9. Hardening: clean-room install test, dogfooding, secret scan, cross-platform CI green
10. **Adversarial review.** Re-read the entire repository as a hostile senior reviewer who has never seen it and is trying to find the lie. Hunt specifically for: numbers that cannot be regenerated, rules with no evidence, estimates presented as measurements, README claims the code does not support, and anything that breaks on a machine that is not this one. Fix everything found. Record it in `RUNLOG.md`.
11. Launch assets in `launch/`: a Show HN post (title plus ≤200 words, leading with the finding, no hype, no emoji), an r/devops post, a LinkedIn post, and a three-tweet thread. Every number traceable to the dataset.
12. `SUMMARY.md`: what was built, what was verified and how, what was cut and why, known limitations, and the exact commands a human should run first to check your work.

---

# HOW THIS WILL BE JUDGED

A stranger clones the repository on a machine you have never seen, runs one install command and one audit command against their own repository, and gets a correct, specific, non-obvious answer about where their CI money goes — with nothing in the README that turns out to be untrue.

Begin with Phase 0. Do not ask me anything.
