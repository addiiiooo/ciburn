## What

## Why

## Checklist
- [ ] tests added (positive + negative fixture for any rule change)
- [ ] golden files regenerated and reviewed by hand
- [ ] no price hardcoded; `pricing.yaml` carries `source_url` + `fetched_at`
- [ ] every estimate states its method
- [ ] `uv run ruff check . && uv run mypy && uv run pytest` pass
