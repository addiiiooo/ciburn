"""Static rules about triggers and workflow-level configuration.

W000 unparsable workflow, W001 missing concurrency cancel, W004 missing path
filters, W008 over-frequent cron, W009 overlapping push+pull_request, W010 no
draft/docs guard on expensive jobs.
"""

from __future__ import annotations

import re
from typing import Any

from ciburn.cron import runs_per_day
from ciburn.findings import Confidence, Finding, Remediation, Severity
from ciburn.rules.base import Context, advisory, config_evidence
from ciburn.workflow import EXPR_RE, Job, Workflow

DOCS_URL_CONCURRENCY = "https://docs.github.com/en/actions/using-jobs/using-concurrency"
DOCS_URL_PATHS = (
    "https://docs.github.com/en/actions/writing-workflows/workflow-syntax-for-github-actions"
    "#onpushpull_requestpull_request_targetpathspaths-ignore"
)
DOCS_URL_SCHEDULE = (
    "https://docs.github.com/en/actions/writing-workflows/choosing-when-your-workflow-runs/"
    "events-that-trigger-workflows#schedule"
)
DOCS_URL_DRAFT = (
    "https://docs.github.com/en/actions/writing-workflows/choosing-when-your-workflow-runs/"
    "events-that-trigger-workflows#pull_request"
)

PR_EVENTS = ("pull_request", "pull_request_target")
EXPENSIVE_STEP_RE = re.compile(
    r"\b(pytest|npm test|yarn test|pnpm test|cargo test|go test|mvn|gradle|dotnet test|"
    r"make test|tox|nox|playwright|cypress|e2e|integration|docker build|docker compose|"
    r"cargo build|bazel|cmake|xcodebuild|flutter|jest|vitest)\b",
    re.IGNORECASE,
)


class W000:
    id = "W000"
    title = "Workflow file could not be parsed"
    severity = Severity.LOW
    needs_history = False

    def run(self, ctx: Context) -> list[Finding]:
        out: list[Finding] = []
        for wf in ctx.workflows:
            if wf.parse_error is None:
                continue
            out.append(
                advisory(
                    self,
                    wf,
                    None,
                    f"{wf.path} could not be parsed ({wf.parse_error}); no rules were applied to it.",
                    Remediation(
                        summary="Fix the YAML so the workflow can be analysed.",
                        docs_url="https://github.com/rhysd/actionlint",
                    ),
                    [config_evidence(wf, error=wf.parse_error)],
                    confidence=Confidence.HIGH,
                )
            )
        return out


class W001:
    id = "W001"
    title = "PR-triggered workflow without concurrency cancel-in-progress"
    severity = Severity.MEDIUM
    needs_history = False

    def run(self, ctx: Context) -> list[Finding]:
        out: list[Finding] = []
        for wf in ctx.parsed_workflows:
            if not wf.has_event(*PR_EVENTS) or wf.is_reusable:
                continue
            if _cancels_in_progress(wf.concurrency):
                continue
            jobs = list(wf.jobs.values())
            if jobs and all(_cancels_in_progress(j.concurrency) for j in jobs):
                continue
            has_group_only = isinstance(wf.concurrency, dict | str)
            msg = (
                f"{wf.path} runs on {', '.join(e for e in wf.events if e in PR_EVENTS)} but "
                + (
                    "its `concurrency` block does not set `cancel-in-progress: true`; "
                    if has_group_only
                    else "has no `concurrency` block; "
                )
                + "every new push to a pull request starts a fresh run while the previous run "
                "keeps burning minutes until it finishes."
            )
            out.append(
                advisory(
                    self,
                    wf,
                    None,
                    msg,
                    Remediation(
                        summary="Add a concurrency group keyed on the PR/ref with cancel-in-progress.",
                        patch={
                            "op": "add_workflow_key",
                            "key": "concurrency",
                            "value": {
                                "group": "${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}",
                                "cancel-in-progress": True,
                            },
                        },
                        docs_url=DOCS_URL_CONCURRENCY,
                    ),
                    [config_evidence(wf, events=wf.events, concurrency=wf.concurrency)],
                    confidence=Confidence.HIGH,
                )
            )
        return out


def _cancels_in_progress(conc: Any) -> bool:
    if not isinstance(conc, dict):
        return False
    v = conc.get("cancel-in-progress")
    if v is True:
        return True
    if isinstance(v, str):
        # an expression such as ${{ github.event_name == 'pull_request' }}: treat as present
        return bool(EXPR_RE.search(v)) or v.strip().lower() == "true"
    return False


class W004:
    id = "W004"
    title = "push/pull_request trigger without paths filter on a repository with separable trees"
    severity = Severity.LOW
    needs_history = False

    def run(self, ctx: Context) -> list[Finding]:
        out: list[Finding] = []
        if ctx.layout is None:
            return out
        trees = ctx.layout.separable_trees
        if not trees:
            return out
        for wf in ctx.parsed_workflows:
            if wf.is_reusable or _looks_like_docs_workflow(wf):
                continue
            unfiltered = [
                ev
                for ev in ("push", "pull_request")
                if ev in wf.on and not _has_path_filter(wf.on[ev])
            ]
            if not unfiltered:
                continue
            if not any(_job_is_code_work(j) for j in wf.jobs.values()):
                continue
            out.append(
                advisory(
                    self,
                    wf,
                    None,
                    f"{wf.path} runs on {', '.join(unfiltered)} for every change, but this repository "
                    f"has separable trees ({', '.join(trees)}); changes that only touch them still "
                    "pay for a full run.",
                    Remediation(
                        summary="Add `paths-ignore` for documentation-only trees (or `paths` for the code).",
                        patch={
                            "op": "add_trigger_key",
                            "events": unfiltered,
                            "key": "paths-ignore",
                            "value": trees,
                        },
                        docs_url=DOCS_URL_PATHS,
                    ),
                    [config_evidence(wf, events=unfiltered, separable_trees=trees)],
                    confidence=Confidence.MEDIUM,
                )
            )
        return out


def _has_path_filter(cfg: dict[str, Any]) -> bool:
    return any(k in cfg for k in ("paths", "paths-ignore"))


def _looks_like_docs_workflow(wf: Workflow) -> bool:
    name = (wf.name or "") + " " + wf.path
    return bool(re.search(r"\b(docs?|documentation|pages|mkdocs|sphinx|website)\b", name, re.I))


def _job_is_code_work(job: Job) -> bool:
    if job.is_reusable_call:
        return True
    for s in job.steps:
        if s.run and EXPENSIVE_STEP_RE.search(s.run):
            return True
        if s.action_repo and s.action_repo.startswith(("actions/setup-", "astral-sh/setup-uv")):
            return True
    return len(job.steps) > 2


class W008:
    id = "W008"
    title = "Scheduled workflow fires more often than the repository is likely to change"
    severity = Severity.LOW
    needs_history = False
    threshold_per_day = 1.0

    def run(self, ctx: Context) -> list[Finding]:
        out: list[Finding] = []
        for wf in ctx.parsed_workflows:
            for cron in wf.crons:
                rpd = runs_per_day(cron)
                if rpd is None or rpd <= self.threshold_per_day:
                    continue
                sev = Severity.MEDIUM if rpd >= 24 else Severity.LOW
                out.append(
                    advisory(
                        self,
                        wf,
                        None,
                        f"{wf.path} is scheduled with `{cron}`, about {rpd:.1f} runs/day. Each firing "
                        "costs a full run whether or not anything changed since the last one.",
                        Remediation(
                            summary="Lower the cron frequency, or guard the job on new commits since the last run.",
                            patch={"op": "advise", "cron": cron},
                            docs_url=DOCS_URL_SCHEDULE,
                        ),
                        [config_evidence(wf, cron=cron, runs_per_day=round(rpd, 2))],
                        severity=sev,
                        confidence=Confidence.MEDIUM,
                    )
                )
        return out


class W009:
    id = "W009"
    title = "push and pull_request triggers overlap: one commit runs twice"
    severity = Severity.MEDIUM
    needs_history = False

    def run(self, ctx: Context) -> list[Finding]:
        out: list[Finding] = []
        for wf in ctx.parsed_workflows:
            if "push" not in wf.on or not wf.has_event(*PR_EVENTS):
                continue
            push = wf.on["push"]
            if not _push_hits_feature_branches(push):
                continue
            out.append(
                advisory(
                    self,
                    wf,
                    None,
                    f"{wf.path} runs on every `push` and on `pull_request`; a push to a PR branch "
                    "from the same repository triggers both, so one commit is built twice.",
                    Remediation(
                        summary="Restrict `push` to the default branch (and tags), leaving PRs to `pull_request`.",
                        patch={
                            "op": "add_trigger_key",
                            "events": ["push"],
                            "key": "branches",
                            "value": ["main"],
                        },
                        docs_url=DOCS_URL_PATHS,
                    ),
                    [
                        config_evidence(
                            wf, push=push, pr_events=[e for e in wf.events if e in PR_EVENTS]
                        )
                    ],
                    confidence=Confidence.HIGH,
                )
            )
        return out


def _push_hits_feature_branches(push: dict[str, Any]) -> bool:
    if "branches-ignore" in push:
        return True  # ignores some, still hits the rest
    if "tags" in push and "branches" not in push:
        return False  # tag-only push trigger
    branches = push.get("branches")
    if branches is None:
        return True
    if isinstance(branches, str):
        branches = [branches]
    if not isinstance(branches, list):
        return True
    for b in branches:
        s = str(b)
        if s in ("*", "**") or s.endswith("/**") or s.endswith("/*") or "*" in s:
            return True
    return False


class W010:
    id = "W010"
    title = "Expensive PR job runs for draft pull requests with no guard"
    severity = Severity.LOW
    needs_history = False

    def run(self, ctx: Context) -> list[Finding]:
        out: list[Finding] = []
        for wf in ctx.parsed_workflows:
            if not wf.has_event(*PR_EVENTS):
                continue
            pr_cfg = wf.on.get("pull_request") or wf.on.get("pull_request_target") or {}
            types = pr_cfg.get("types")
            if (
                isinstance(types, list)
                and "ready_for_review" in types
                and "synchronize" not in types
            ):
                continue
            if _guards_draft(wf.raw.get("if") if isinstance(wf.raw, dict) else None):
                continue
            for job in wf.jobs.values():
                if not _is_expensive(job):
                    continue
                if _guards_draft(job.if_):
                    continue
                if _has_path_filter(pr_cfg):
                    continue
                out.append(
                    advisory(
                        self,
                        wf,
                        job,
                        f"Job `{job.key}` in {wf.path} looks expensive ({_expensive_reason(job)}) and runs "
                        "on every push to every pull request, including drafts and docs-only changes.",
                        Remediation(
                            summary="Skip drafts with `if: github.event.pull_request.draft == false` and/or add `paths-ignore`.",
                            patch={
                                "op": "add_job_key",
                                "job": job.key,
                                "key": "if",
                                "value": "github.event.pull_request.draft == false",
                            },
                            docs_url=DOCS_URL_DRAFT,
                        ),
                        [config_evidence(wf, job, reason=_expensive_reason(job))],
                        confidence=Confidence.LOW,
                    )
                )
        return out


def _guards_draft(cond: str | None) -> bool:
    return bool(cond and "draft" in cond)


def _expensive_reason(job: Job) -> str:
    """Why a job is considered expensive enough to guard. Test steps alone do not
    qualify: a single Linux pytest job is the normal case, not waste."""
    reasons: list[str] = []
    if job.matrix_legs and job.matrix_legs >= 3:
        reasons.append(f"matrix of {job.matrix_legs} legs")
    if any(lb.lower().startswith(("macos", "windows")) for lb in job.runs_on):
        reasons.append("macOS/Windows runner")
    if job.services or job.container:
        reasons.append("service/containers")
    has_tests = any(s.run and EXPENSIVE_STEP_RE.search(s.run) for s in job.steps)
    if has_tests and job.timeout_minutes is not None and job.timeout_minutes >= 30:
        reasons.append(f"test/build steps with a {job.timeout_minutes}-minute timeout")
    return ", ".join(reasons) or "unknown"


def _is_expensive(job: Job) -> bool:
    return _expensive_reason(job) != "unknown"
