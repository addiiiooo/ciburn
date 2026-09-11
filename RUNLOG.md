# RUNLOG.md

Gate-by-gate log. Written so that the next phase can be executed by someone who
has read nothing else. Newest at the bottom.

## Gate 1 — Phase 0 research (2026-09-11 18:00–19:00 UTC)

**Built:** `RESEARCH.md`, `DECISIONS.md` (D001–D008), `src/ciburn/data/pricing.yaml`
(three models, 36 SKUs each, label→SKU regexes, limits, quotas, rounding rule),
recorded API fixtures in `tests/fixtures/api/` (runs, jobs, workflows, timing,
billing-404, rate-limit headers), `.gitignore`, `LICENSE`.

**Verified:** name `ciburn` free on PyPI/GitHub; no incumbent tool (no PIVOT);
rounding rule verbatim from docs; 2026 rates from docs `main`; 2025 rates from
`github/docs` commits `ce33310edb3d` and `9075b64ffbbe`; self-hosted charge
postponed (docs say self-hosted is free as of today); `/timing` returns 0 ms for
public repos and is closing down; ICSE 2024 figures checked against the PDF
(one misquote in the brief corrected).

**Corrections to the brief:** (1) hosted prices went *down* on 2026-01-01, the
platform charge is folded into the rates; (2) the self-hosted charge is not in
effect; (3) "13 % of scheduled workflows fail consecutively" is actually "13 % of
scheduled runs fail".

**Remains:** everything after Phase 0. Next: Gate 2 skeleton + packaging + CI.

**Environment for the next phase:** Homebrew `python3.11`, `uv 0.12.13`; the
API token is obtained per-command with
`git credential fill` (see D001) and exported as `GITHUB_TOKEN` in-process only.
