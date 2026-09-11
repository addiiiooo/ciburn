"""Static rules about matrices: W005 rounding-dominated / redundant legs, W012 fail-fast off."""

from __future__ import annotations

import json
from typing import Any

from ciburn.findings import Confidence, Finding, Remediation, Severity
from ciburn.rules.base import Context, advisory, config_evidence

DOCS_URL_MATRIX = "https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/running-variations-of-jobs-in-a-workflow"
DOCS_URL_ROUNDING = "https://docs.github.com/en/billing/reference/actions-runner-pricing"


class W005:
    id = "W005"
    title = (
        "Matrix with redundant legs, or legs short enough that per-job minute rounding dominates"
    )
    severity = Severity.LOW
    needs_history = False
    wide_threshold = 6

    def run(self, ctx: Context) -> list[Finding]:
        out: list[Finding] = []
        for wf in ctx.parsed_workflows:
            for job in wf.jobs.values():
                if job.matrix is None:
                    continue
                dups = _duplicate_legs(job.matrix)
                legs = job.matrix_legs
                if dups:
                    out.append(
                        advisory(
                            self,
                            wf,
                            job,
                            f"Job `{job.key}` in {wf.path} has a matrix with duplicate legs "
                            f"({', '.join(dups[:3])}); each duplicate is a full extra job.",
                            Remediation(
                                summary="Remove the duplicated matrix values.",
                                patch={"op": "advise", "job": job.key, "duplicates": dups},
                                docs_url=DOCS_URL_MATRIX,
                            ),
                            [config_evidence(wf, job, duplicates=dups)],
                            confidence=Confidence.HIGH,
                            severity=Severity.MEDIUM,
                        )
                    )
                    continue
                if legs is not None and legs >= self.wide_threshold:
                    out.append(
                        advisory(
                            self,
                            wf,
                            job,
                            f"Job `{job.key}` in {wf.path} expands to {legs} matrix legs. GitHub bills each "
                            "leg rounded up to a whole minute, so short legs pay up to 59 s of rounding "
                            f"each: {legs} legs can bill up to {legs} extra minutes per run.",
                            Remediation(
                                summary="Merge short legs (e.g. run several versions in one job) or prune the matrix.",
                                patch={"op": "advise", "job": job.key, "legs": legs},
                                docs_url=DOCS_URL_ROUNDING,
                            ),
                            [config_evidence(wf, job, legs=legs, matrix=_short_matrix(job.matrix))],
                            confidence=Confidence.LOW,
                        )
                    )
        return out


def _duplicate_legs(matrix: dict[str, Any]) -> list[str]:
    dups: list[str] = []
    for axis, values in matrix.items():
        if axis in ("include", "exclude") or not isinstance(values, list):
            continue
        seen: set[str] = set()
        for v in values:
            key = json.dumps(v, sort_keys=True, default=str)
            if key in seen:
                dups.append(f"{axis}={v}")
            seen.add(key)
    include = matrix.get("include")
    if isinstance(include, list):
        seen_inc: set[str] = set()
        for inc in include:
            key = json.dumps(inc, sort_keys=True, default=str)
            if key in seen_inc:
                dups.append(f"include={key}")
            seen_inc.add(key)
    return dups


def _short_matrix(matrix: dict[str, Any]) -> dict[str, Any]:
    return {
        k: (v if not isinstance(v, list) else v[:8])
        for k, v in matrix.items()
        if k not in ("include", "exclude")
    }


class W012:
    id = "W012"
    title = "Matrix with fail-fast disabled keeps sibling legs running after the first failure"
    severity = Severity.LOW
    needs_history = False
    min_legs = 4

    def run(self, ctx: Context) -> list[Finding]:
        out: list[Finding] = []
        for wf in ctx.parsed_workflows:
            if not wf.has_event("pull_request", "pull_request_target", "push"):
                continue
            for job in wf.jobs.values():
                if job.matrix is None or job.fail_fast is not False:
                    continue
                legs = job.matrix_legs
                if legs is not None and legs < self.min_legs:
                    continue
                out.append(
                    advisory(
                        self,
                        wf,
                        job,
                        f"Job `{job.key}` in {wf.path} sets `fail-fast: false` on a "
                        f"{legs or 'multi'}-leg matrix; when one leg fails on a PR, the other legs run to "
                        "completion and are billed even though the run is already red.",
                        Remediation(
                            summary="Use `fail-fast: ${{ github.event_name == 'pull_request' }}` to keep full results on main only.",
                            patch={
                                "op": "set_strategy_key",
                                "job": job.key,
                                "key": "fail-fast",
                                "value": "${{ github.event_name == 'pull_request' }}",
                            },
                            docs_url=DOCS_URL_MATRIX,
                        ),
                        [config_evidence(wf, job, legs=legs)],
                        confidence=Confidence.MEDIUM,
                    )
                )
        return out
