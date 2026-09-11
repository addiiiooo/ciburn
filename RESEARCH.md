# RESEARCH.md — Phase 0: verify the world before building

All fetches were performed on **2026-09-11** between 18:00 and 18:50 UTC from this
machine. Where a page was fetched through the GitHub REST API or raw
`github/docs` source, the exact commit SHA is given so the reading can be
reproduced. Anything marked **CORRECTION** contradicts the brief that started
this project; Phase 0 wins over the brief.

## 0.1 Prior-art check

Searched GitHub (repository search, sorted by stars), PyPI, and the web for tools
that join Actions run history to cost. Star counts and last-push dates were read
from `GET /repos/{owner}/{repo}` on 2026-09-11.

| Tool | Stars | Last push | What it does | Gap it leaves |
|---|---|---|---|---|
| [rhysd/actionlint](https://github.com/rhysd/actionlint) | 4,215 | 2026-07-16 | Static syntax/semantics checker for workflow files | No run history, no cost |
| [zizmorcore/zizmor](https://github.com/zizmorcore/zizmor) | 6,480 | 2026-09-11 | Static security analysis for workflows | No run history, no cost |
| [self-actuated/actions-usage](https://github.com/self-actuated/actions-usage) | 191 | 2024-05-01 | CLI summing total minutes from the REST API | Totals only; no rules, no cost attribution, no 2026 pricing; unmaintained 16 months |
| [fkirc/skip-duplicate-actions](https://github.com/fkirc/skip-duplicate-actions) | 535 | 2026-07-28 | Runtime action that skips duplicate runs | A remediation, not an analyzer |
| [fchimpan/gh-slimify](https://github.com/fchimpan/gh-slimify) | 111 | 2026-07-24 | Migrates eligible jobs to `ubuntu-slim` | Single remediation, no history |
| [builtbyadam/runner-cost-reporter](https://github.com/builtbyadam/runner-cost-reporter) | 2 | 2026-08-27 | Minutes per workflow over a window | No rules, no pricing models |
| [jlaportebot/gh-actions-optimizer](https://github.com/jlaportebot/gh-actions-optimizer) | 0 | 2026-06-24 | "cost analyzer and optimizer" | No users, no history join found |
| CICosts ([phonotechnologies/cicosts-app](https://github.com/phonotechnologies/cicosts-app)) | 12 | 2026-03-16 | Hosted GitHub App + dashboard, webhook driven | Hosted service; not offline; no static rules |

Academic prior art:

- Bouzenia & Pradel, *Resource Usage and Optimization Opportunities in Workflows
  of GitHub Actions*, ICSE 2024. PDF:
  <https://software-lab.org/publications/icse2024_workflows.pdf>. Dataset:
  952 repositories, 1.3 million workflow runs, 30 months.
- *On the Reruns of GitHub Actions Workflows*, TOSEM 2026,
  <https://dl.acm.org/doi/10.1145/3795771> (rerun behaviour; relevant to H006).

**Verdict:** no mature, actively maintained tool (>1,000 stars, commits within 90
days) does history-attributed cost analysis. `actionlint` and `zizmor` are the
mature neighbours and are cited as such. No `PIVOT.md` is needed. What ciburn
can claim as novel: the join of workflow configuration to that repository's own
run history and money, pricing under more than one 2025/2026 model, and the
public corpus dataset.

## 0.2 Pricing facts (all from primary GitHub sources)

### Rounding rule — verified, load-bearing

> "GitHub rounds the minutes and partial minutes each job uses up to the nearest
> whole minute."

Source: <https://docs.github.com/en/billing/reference/actions-runner-pricing>
(also present verbatim in every historical revision of the source file
`content/billing/reference/actions-runner-pricing.md` in `github/docs`, e.g.
commits `3d77b7e3ca33` 2025-10-30, `ce33310edb3d` 2025-12-16,
`260144b4be97` 2026-01-05, and `main` on 2026-09-11).

Consequence: a job that runs 61 seconds bills 2 minutes; a 20-leg matrix of
70-second jobs bills 40 minutes for 23.3 minutes of work. This is what W005 and
the pricing engine's property tests are built on.

### Per-minute rates — verified

**In effect from 2026-01-01** (source: docs page above, `main` on 2026-09-11):

| SKU | OS | Rate/min |
|---|---|---|
| `actions_linux_slim` (1-core) | Linux | $0.002 |
| `actions_linux` (2-core x64) | Linux | $0.006 |
| `actions_linux_arm` (2-core arm64) | Linux | $0.005 |
| `actions_windows` (2-core x64) | Windows | $0.010 |
| `actions_windows_arm` (2-core arm64) | Windows | $0.010 |
| `actions_macos` (3/4-core) | macOS | $0.062 |
| larger runners `linux_4_core` … `windows_96_core`, `macos_l`, `macos_xl`, GPU | see `pricing.yaml` | $0.006 – $0.552 |

**In effect until 2025-12-31** (source: `github/docs` commit `ce33310edb3d`,
which carried a "Per-minute rate until January 1, 2026 / January 1, 2026 onward"
comparison table for larger runners, and commit `9075b64ffbbe` of
`content/billing/reference/actions-minute-multipliers.md` for standard runners):

| SKU | Rate/min (2025) |
|---|---|
| Linux 2-core | $0.008 |
| Windows 2-core | $0.016 |
| macOS 3/4-core | $0.080 |
| Linux 1-core | $0.002 |
| Linux 2-core arm64 | $0.005 |
| Windows 2-core arm64 | $0.010 |
| larger runners | 20–39 % higher than 2026, full table in `pricing.yaml` |

**CORRECTION to the brief:** the brief said "GitHub repriced hosted runners on
2026-01-01 and introduced a $0.002/minute Actions platform charge". What GitHub
actually did on 2026-01-01 was *reduce* hosted-runner prices by up to 39 %. The
$0.002/min platform charge exists but "for GitHub-hosted runners, the new Actions
cloud platform charge is already included into the reduced meter price"
(<https://github.com/resources/insights/2026-pricing-changes-for-github-actions>).
It is therefore never added on top of a SKU rate in ciburn.

**CORRECTION to the brief:** "from 2026-03-01 self-hosted runner minutes are
billed for the first time" is **not in effect**. The self-hosted charge was
announced on 2025-12-16
(<https://github.blog/changelog/2025-12-16-coming-soon-simpler-pricing-and-a-better-experience-for-github-actions/>)
and postponed the next day. The changelog now opens with: "Update: We've read
your posts and heard your feedback. We're postponing the announced billing
change for self-hosted GitHub Actions to take time to re-evaluate our approach."
(post `dateModified` 2025-12-17T21:50:05Z). The current billing concepts page
states: "GitHub Actions usage is **free** for **self-hosted runners** and for
**public repositories** that use standard GitHub-hosted runners"
(<https://docs.github.com/en/billing/concepts/product-billing/github-actions>,
`github/docs` `main` 2026-09-11). No later changelog reinstating the charge was
found on github.blog or docs.github.com as of the fetch date. ciburn ships the
announced model as an explicit, clearly labelled **scenario**
(`--model 2026-selfhosted-announced`) so a team can see what it would cost if
it lands; it is never used by default.

### OS multipliers — CORRECTION

The old "Linux 1×, Windows 2×, macOS 10×" minute-multiplier table no longer
exists in GitHub's documentation. The article
`content/billing/reference/actions-minute-multipliers.md` already listed per-SKU
dollar rates by 2025-08-14 (commit `9075b64ffbbe`) and was renamed to
"Actions runner pricing" on 2025-10-30 (commit `3d77b7e3ca33`). ciburn prices
by SKU rate, not by multiplier. How Windows/macOS minutes draw down the
*included* quota is not stated on any current primary page; see DECISIONS.md
D005 for the assumption ciburn uses and how it is labelled.

### Included minutes per plan — verified

Free 2,000; Pro 3,000; Free for organizations 2,000; Team 3,000; Enterprise Cloud
50,000 minutes/month. "Included minutes cannot be used for larger runners."
"Larger runners are always charged for, even when used by public repositories."
Source: <https://docs.github.com/en/billing/concepts/product-billing/github-actions>
(reusable `data/reusables/billing/actions-included-quotas.md`).

### GitHub's "96 % of customers" statement — verified wording

"96% of customers will see no change to their bill. Of the 4% of Actions users
impacted by this change, 85% of this cohort will see their Actions bill decrease
and the remaining 15% who are impacted across all face a median increase around
$13." Source: <https://github.com/resources/insights/2026-pricing-changes-for-github-actions>
(original 2025-12-16 announcement text, which included the self-hosted charge
in its modelling). The corpus study can only speak to hosted-runner list-price
deltas for public repositories priced *as if private*; it cannot see anyone's
bill, included-minute consumption, or self-hosted usage. REPORT.md says so.

### Runner labels and hardware — verified

<https://docs.github.com/en/actions/reference/runners/github-hosted-runners>:
`ubuntu-slim` 1 vCPU; `ubuntu-latest`/`ubuntu-24.04`/`ubuntu-22.04` 4 vCPU in
public repos, 2 vCPU in private repos; `windows-latest`/`windows-2025`/
`windows-2022` likewise; `macos-latest`/`macos-15`/`macos-14` 3-core M1 arm64;
`macos-15-intel` 4-core Intel. `ubuntu-slim`: "The job timeout for single-CPU
runners is 15 minutes."
<https://docs.github.com/en/actions/reference/runners/larger-runners>: macOS
large = 12-core Intel (`macos-*-large`, SKU `macos_l`); xlarge = 5-core M2
(`macos-*-xlarge`, `xcode-*-xlarge`, SKU `macos_xl`). Linux/Windows larger
runners use org-defined custom labels and cannot be identified from the label.

### Limits — verified

<https://docs.github.com/en/actions/reference/limits>: "Each job in a workflow
can run for up to 6 hours of execution time" (the 360-minute default behind
W003/H007); self-hosted jobs up to 5 days; workflow run limit 35 days; matrix
max 256 jobs; `GITHUB_TOKEN` rate limit 1,000 requests/hour/repository.

## 0.3 API surface (verified against docs.github.com and live calls)

API version header used everywhere: `X-GitHub-Api-Version: 2022-11-28`.
Recorded live responses are committed under `tests/fixtures/api/` (public data,
no tokens).

| Need | Endpoint | Verified facts |
|---|---|---|
| List workflows | `GET /repos/{o}/{r}/actions/workflows` | `state` ∈ active, deleted, disabled_fork, disabled_inactivity, disabled_manually. Fixture `workflows_astral_ruff.json`. |
| List runs | `GET /repos/{o}/{r}/actions/runs` | params `per_page`(≤100), `status`, `created` (date range), `branch`, `event`, `exclude_pull_requests`, `head_sha`. Fields incl. `run_attempt`, `run_started_at`, `event`, `conclusion`, `head_sha`, `path`, `workflow_id`. `total_count` caps at pagination; `Link` header for paging. Fixture `runs_pallets_flask.json`. |
| Jobs for a run | `GET /repos/{o}/{r}/actions/runs/{id}/jobs?filter=all\|latest` | per-job `started_at`, `completed_at`, `conclusion` (incl. `timed_out`), `labels[]`, `runner_name`, `runner_group_name`, `run_attempt`, `steps[]` with `started_at`/`completed_at`. Fixtures `jobs_pallets_flask.json`, `jobs_astral_ruff_depot.json` (third-party runner labels). |
| Run usage | `GET /repos/{o}/{r}/actions/runs/{id}/timing` | **Closing down** (<https://github.blog/changelog/2025-02-02-actions-get-workflow-usage-and-get-workflow-run-usage-endpoints-closing-down/>). Still answers 200 on 2026-09-11 but returns `total_ms: 0` for public repositories ("Billable minutes only apply to workflows in private repositories that use GitHub-hosted runners"). Fixtures `timing_*.json`. |
| Workflow usage | `GET …/workflows/{id}/timing` | Same closing-down notice; returned `{"billable":{}}` for a public repo. |
| Billing usage | `GET /users/{u}/settings/billing/usage`, `GET /organizations/{org}/settings/billing/usage` | Enhanced billing platform only; returned 404 for this account. Fields `usageItems[].{date,product,sku,quantity,unitType,pricePerUnit,grossAmount,discountAmount,netAmount,repositoryName}`. Optional in ciburn, never required. |
| Rate limits | `GET /rate_limit` | Unauthenticated 60/h; authenticated 5,000/h; search 30/min; secondary limit 900 points/min, 100 concurrent; 403/429 with `retry-after` or `x-ratelimit-reset`. <https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api> |

**Design consequence:** billable minutes cannot be read from GitHub for public
repositories and the usage endpoints are being retired. ciburn therefore
computes minutes from job `started_at`/`completed_at` timestamps (the same
quantity GitHub rounds up per job) and labels the result "observed minutes".
For private repositories the tool reconciles against `/timing` when it returns
non-zero data and reports the delta.

## 0.4 Name check — `ciburn` is free

On 2026-09-11: `https://pypi.org/pypi/ciburn/json` → 404;
`GET /repos/addiiiooo/ciburn` → 404; repository search for "ciburn" → 0 results.
Fallbacks `runnerburn`, `ciwaste`, `actionspend`, `burnmeter` were all free too.

## 0.5 ICSE 2024 figures — verified against the PDF text

Extracted with `pdfminer.six` from the PDF above; page/section references are to
that text.

| Claim in the brief | Paper says | Status |
|---|---|---|
| 1.3 M runs, 952 repos | "952 repositories that performed 1.3 million workflow runs over a period of 30 months" (Abstract/§1) | verified |
| 32.9 % of paid-tier repos use caching | "caching, which is used by 32.9% of paid-tier repositories and reduces VM time by 3.4%" (§1 RQ2); free tier 17.8 % (§4.2) | verified |
| 14 % set custom timeout | "setting a timeout value for a job, which is used by 14.0% of paid-tier repositories, impacts 4.3% of runs, and reduces VM time by 8.1%" (§1 RQ2; §4.2.6, default 360 min) | verified |
| failed runs = 30.9 % of paid VM time, 17.4 % of runs | Table 3: paid tier Failure 17.4 % of runs, 30.9 % of VM time | verified |
| "13 % of scheduled workflows fail consecutively" | Paper: "scheduled workflows account for 29.8% of all runs and consume 15.4% of total VM time. Notably, 13% of these scheduled runs result in failure." With k=3, deactivation "impacts 17.2% of scheduled runs" and saves "21.3% of scheduled runs time" (§4.3.1, Table 5) | **CORRECTION**: 13 % is the scheduled-run failure rate, not a consecutive-failure rate |
| ~12,000 consecutive failing scheduled runs | "a scheduled workflow set to execute every 5 minutes … amounting to a staggering 12,000 consecutive failures" (§4.1.4) | verified |
| average paid-tier repo cost | "$504 per year for an average paid-tier repository" (Abstract) | additional, verified |

## Environment facts that shaped decisions

- Host: macOS (Darwin 25.5), Homebrew Python 3.11 and 3.14 available, `uv`
  installed during Phase 0 (0.12.13). No `gh` CLI.
- No `GITHUB_TOKEN` in the environment. The macOS keychain holds a `gho_` OAuth
  token for github.com (scopes `read:user, repo, user:email, workflow`) that
  `git credential fill` returns; it gives 5,000 requests/hour. See DECISIONS.md
  D001 for how it is used and the guarantee that it is never written to disk.
