# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [0.1.0] — unreleased

### Added
- Static rules W000–W012 over `.github/workflows/*.yml` (no network).
- History rules H001–H009 over a repository's own workflow-run history.
- The join: every static finding is measured against observed minutes and
  list-price money from that repository's run history when history is
  available; otherwise it is reported as advisory, never as a measurement.
- Pricing engine driven entirely by `data/pricing.yaml` (2026 rates in effect,
  2025 rates, and the announced-then-postponed self-hosted platform-charge
  scenario), with GitHub's per-job round-up rule and an explicitly labelled
  included-minutes assumption.
- `ciburn audit` (terminal / md / json / html), `ciburn price --compare`,
  `ciburn fix` (unified diff; never commits), `ciburn rules`, `ciburn models`.
- Resumable SQLite history cache; rate-limit aware GitHub REST client.
- `action.yml` composite action that posts the report as a PR comment.
- Corpus scan and report scripts (`scripts/corpus_scan.py`, `scripts/build_report.py`).
