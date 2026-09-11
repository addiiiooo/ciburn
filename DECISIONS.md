# DECISIONS.md

Append-only log of decisions taken without a human. Each entry: what was
decided, alternatives, and why. Newest at the bottom.

## D001 — Use the keychain OAuth token for read-only API calls in this session

**Decision:** Phase 0 found no `GITHUB_TOKEN` but the macOS keychain returns a
`gho_` token for github.com via `git credential fill` (5,000 req/h). It is
exported into the shell only for the duration of a command, is used strictly
for `GET` requests against public repositories, and is never written to any file
in the repository. Fixtures and dataset rows are scanned for `gh[ops]_` prefixes
before every commit.
**Alternatives:** unauthenticated (60 req/h — makes the corpus study
impossible); ask the user (forbidden by the brief).
**Why:** the brief requires authenticated, respectful API use for the corpus
study, and the token is the user's own credential on the user's own machine.

## D002 — Package and repository name: `ciburn`

**Decision:** `ciburn`. Free on PyPI and GitHub (`addiiiooo/ciburn`) on
2026-09-11. Fallbacks were also free; the first candidate is the shortest.

## D003 — Pricing models shipped: `2026` (default), `2025`, `2026-selfhosted-announced`

**Decision:** three named models in `pricing.yaml`. `2026` is the current
list price. `2025` is the pre-2026-01-01 list price, reconstructed from
GitHub-authored docs history. `2026-selfhosted-announced` is the announced and
postponed self-hosted platform charge, kept as an explicitly labelled scenario.
**Alternatives:** only ship the current model (loses the "before/after" answer
the brief asks for); treat the self-hosted charge as real (would be false as of
2026-09-11).
**Why:** Phase 0 contradicted the brief; the tool must reflect what is true now
and still let a team see the announced future.

## D004 — Observed minutes come from job timestamps, not the `/timing` endpoint

**Decision:** minutes per job = `completed_at - started_at`, rounded up per job
to a whole minute (GitHub's rule). `/timing` is queried only for private
repositories, as a reconciliation check, and its result is reported as a delta.
**Alternatives:** rely on `/timing` (returns 0 ms for public repos and is
closing down); rely on the billing usage API (enhanced-billing accounts only,
no per-job data).
**Why:** it is the only quantity available for every repository, it is the same
quantity GitHub rounds, and it is verifiable by anyone with the jobs API.

## D005 — Included-minute drawdown for Windows/macOS is an explicit assumption

**Decision:** cost is reported at list price by default (no quota offset). When
the user passes `--plan`, standard-runner minutes are drawn from the plan's
quota at the ratio `sku_rate / actions_linux_rate` and the report says
"assumption: drawdown at list-price ratio". Larger runners never draw from the
quota (documented).
**Alternatives:** ignore the quota entirely (loses a real offset for small
teams); use the legacy 2×/10× multipliers (no longer documented).
**Why:** the announced 2026 wording says self-hosted minutes would "consume
available usage based on list price the same way that Linux, Windows, and MacOS
standard runners work today", which is the only current statement of the rule.
It is labelled as an assumption everywhere it is applied.

## D006 — Python floor 3.11; CI matrix 3.11/3.12/3.13

**Decision:** `requires-python = ">=3.11"`. The host also has 3.14 and the
suite will be run on it locally, but 3.14 is not in the required matrix.

## D007 — Classifying runners from job metadata

**Decision:** a job is GitHub-hosted if `runner_name` starts with
`GitHub Actions` or `GitHub-hosted`, or if any label matches a hosted pattern in
`pricing.yaml`. Labels matching a hosted pattern map to a SKU. Any other job is
classed `self_hosted_or_custom` (Depot, BuildJet, ARC, org larger-runner labels
all land here): its minutes are observed and shown, but priced at $0 under the
`2026`/`2025` models and at the platform charge under the scenario model. The
user can map custom labels with `--label-sku LABEL=SKU` or a config file.
**Why:** Linux/Windows larger runners use org-defined labels that cannot be
identified from the API; guessing a price would be fabrication.

## D008 — Corpus study runs on public repositories priced "as if private"

**Decision:** public repos are free on standard runners, so every dollar figure
in REPORT.md is "what this usage would cost at private-repository list price".
The report never presents these as bills anyone paid.
