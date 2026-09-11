# RESUME.md — what remains, exactly

State at hand-off: everything in `SUMMARY.md` is committed on `main` locally.
No remote has been added and nothing has been pushed.

## 1. Let the corpus scan finish, then regenerate everything derived from it

The scan was started on 2026-09-11 at 18:37 UTC with the machine's keychain
token exported only into that process:

```bash
ps aux | grep corpus_scan          # is it still running?
tail -3 corpus/scan.log            # "[i/1993] owner/name: ..." lines; "finished:" when done
cat corpus/progress.json
```

If it stopped early, re-run it; it resumes from `corpus/corpus.sqlite` and
`corpus/sample.json`:

```bash
GITHUB_TOKEN=... uv run python scripts/corpus_scan.py --workdir corpus --per-cell 50
```

When it reports `finished`, regenerate the dataset, the report and the launch
posts (all numbers come from the data; nothing is edited by hand):

```bash
uv run python scripts/build_report.py --workdir corpus --out dataset --report REPORT.md
uv run python scripts/build_launch.py
git add dataset REPORT.md launch && git commit -m "data: full corpus scan (N repositories)"
```

Then read `REPORT.md` and `launch/*.md` once more as a hostile reviewer: the
"scanned N of 1993" line must match, and the `_Sample note_` in
`launch/show-hn.md` disappears automatically when the scan is complete.

## 2. Push and watch CI

```bash
git remote add origin git@github.com:addiiiooo/ciburn.git
git push -u origin main
```

The matrix (ubuntu/macos/windows × 3.11/3.12/3.13, lint, package, dogfood)
has never executed. Watch the first run. Likely first-run issues, if any:
Windows console encoding in the dogfood step (it runs on ubuntu, so unlikely),
`uv sync --group dev` resolving `pyarrow-stubs`/`pandas-stubs` on 3.13, and the
dogfood job's `--fail-on medium` against this repository's own history (the
workflow was written to pass W001–W012 statically; H-rules need history).

## 3. Optional follow-ups (not required by the brief)

- Publish to PyPI (`uv build && uv publish`), then switch the README install
  section to `uv tool install ciburn`.
- Tag `v0.1.0` and move the CHANGELOG entry from "unreleased".
- Ingest the artifacts API so W011 can be measured (storage, not minutes).
- Read `/timing` reconciliation for private repositories into the report
  (currently fetched and stored, not displayed).
