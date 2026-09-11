# Contributing to ciburn

Thanks for looking. The bar for changes is the same bar the project holds
itself to: every number a user sees must be traceable to observed data or to a
primary source, and every estimate must say how it was estimated.

## Setup

```bash
git clone https://github.com/addiiiooo/ciburn && cd ciburn
uv sync --group dev
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Python 3.11+ is required. The unit suite makes no network calls; one live
integration test runs only with `CIBURN_LIVE=1`.

## Adding or changing a rule

1. Give it a stable id (`W0xx` static, `H0xx` history), a severity, and a
   confidence policy.
2. Add at least one positive and one negative fixture under
   `tests/fixtures/workflows/` (static) or a synthetic history in
   `tests/test_join_and_history_rules.py` (history).
3. Regenerate golden files with `uv run python scripts/regen_golden.py` and
   **review the diff by hand**; a golden file is a reviewed statement, not a
   snapshot.
4. If the rule produces an estimate, state the method in `estimate_method` and
   cite either a source or the derivation in the rule's docstring.
5. Keep coverage of `ciburn.rules`, `ciburn.pricing` and `ciburn.join` at or
   above 90 %.

## Pricing data

Never hardcode a price. Edit `src/ciburn/data/pricing.yaml`, keep
`source_url` and `fetched_at` on every block, and re-verify against the URLs
listed in `RESEARCH.md`. `tests/test_pricing_freshness.py` fails when the file
is older than 90 days; that is intentional.

## Commits and pull requests

Conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`), one per
unit of work. Never weaken, skip or delete a test to make the suite pass; if a
test is wrong, fix it and explain why in the commit message.

## Scope

Not in scope for v1 (deliberately): a web dashboard or hosted service, CI
providers other than GitHub Actions, any runtime LLM call, auto-merge or
auto-PR, telemetry, a database server, or auth beyond a user-supplied token.
