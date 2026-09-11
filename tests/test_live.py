"""One live integration test against api.github.com. Skipped unless CIBURN_LIVE=1."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ciburn.audit import AuditOptions, run_audit

pytestmark = pytest.mark.live


@pytest.mark.skipif(
    os.environ.get("CIBURN_LIVE") != "1", reason="set CIBURN_LIVE=1 to run against api.github.com"
)
def test_live_audit_small_public_repo(tmp_path: Path) -> None:
    res = run_audit(
        AuditOptions(
            repo="pallets/flask",
            days=7,
            cache=tmp_path / "c.sqlite",
            max_job_runs=5,
            fetch_commits=False,
        )
    )
    assert res.error is None, res.error
    assert res.history is not None
    assert res.totals is not None
    # internal consistency: totals reconcile with the sum over jobs
    assert res.totals.billed_minutes == sum(j.billed_minutes for j in res.history.jobs)
    assert float(res.totals.cost) == pytest.approx(
        sum(float(j.cost) for j in res.history.jobs), abs=1e-6
    )
    d = res.as_dict()
    assert d["tool"]["name"] == "ciburn"
