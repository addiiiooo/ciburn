"""Static rules about individual jobs: W002 cache, W003 timeout, W006 fetch-depth,
W007 oversized runner, W011 artifact retention."""

from __future__ import annotations

import re
from typing import Any

from ciburn.findings import Confidence, Finding, Remediation, Severity
from ciburn.rules.base import Context, advisory, config_evidence
from ciburn.workflow import Job, Step, Workflow

DOCS_URL_CACHE = (
    "https://docs.github.com/en/actions/using-workflows/caching-dependencies-to-speed-up-workflows"
)
DOCS_URL_TIMEOUT = (
    "https://docs.github.com/en/actions/writing-workflows/workflow-syntax-for-github-actions"
    "#jobsjob_idtimeout-minutes"
)
DOCS_URL_CHECKOUT = "https://github.com/actions/checkout#usage"
DOCS_URL_SLIM = "https://docs.github.com/en/actions/reference/runners/github-hosted-runners"
DOCS_URL_ARTIFACT = "https://github.com/actions/upload-artifact#retention-period"

INSTALL_RE = re.compile(
    r"(\bpip3?\s+install\b|\buv\s+(sync|pip\s+install)\b|\bpoetry\s+install\b|\bpipenv\s+(install|sync)\b|"
    r"\bnpm\s+(ci|install|i)\b|\byarn(\s+install)?\s*$|\byarn\s+install\b|\bpnpm\s+(i|install)\b|"
    r"\bbun\s+install\b|\bcargo\s+(build|test|fetch)\b|\bgo\s+(build|test|mod\s+download)\b|"
    r"\bbundle\s+install\b|\bcomposer\s+install\b|\bmvn\b|\bgradle\w*\b|\bdotnet\s+restore\b|"
    r"\bconda\s+(install|env\s+create)\b|\bmamba\b|\bnix\s+build\b|\bapt(-get)?\s+install\b)",
    re.IGNORECASE | re.MULTILINE,
)
# Actions that cache by themselves (or by default). Value: input that disables it, if any.
CACHING_ACTIONS: dict[str, str | None] = {
    "actions/cache": None,
    "actions/cache/restore": None,
    "swatinem/rust-cache": None,
    "astral-sh/setup-uv": "enable-cache",
    "actions/setup-go": "cache",  # default true since v4
    "gradle/actions": None,
    "gradle/gradle-build-action": None,
    "ruby/setup-ruby": None,  # only with bundler-cache: true (checked below)
    "jdx/mise-action": "cache",
    "hendrikmuhs/ccache-action": None,
    "mozilla-actions/sccache-action": None,
    "determinatesystems/magic-nix-cache-action": None,
    "cachix/cachix-action": None,
    "bazel-contrib/setup-bazel": None,
    "mlugg/setup-zig": "use-cache",
    "docker/build-push-action": None,  # only with cache-from (checked below)
    "buildjet/cache": None,
    "runs-on/cache": None,
    "actions/setup-dotnet": "cache",
}
SETUP_WITH_CACHE_INPUT = {
    "actions/setup-node": "cache",
    "actions/setup-python": "cache",
    "actions/setup-java": "cache",
    "actions/setup-dotnet": "cache",
}
LIGHT_ACTIONS = {
    "actions/checkout",
    "actions/labeler",
    "actions/stale",
    "actions/github-script",
    "actions/first-interaction",
    "actions/add-to-project",
    "dependabot/fetch-metadata",
    "release-drafter/release-drafter",
    "amannn/action-semantic-pull-request",
    "actions/dependency-review-action",
    "peter-evans/create-or-update-comment",
    "peter-evans/find-comment",
    "actions-cool/issues-helper",
    "actions/upload-artifact",
    "actions/download-artifact",
    "actions/setup-python",
    "actions/setup-node",
    "astral-sh/setup-uv",
    "pre-commit/action",
    "actions/cache",
    "zizmorcore/zizmor-action",
    "rhysd/actionlint",
    "reviewdog/action-actionlint",
    "codespell-project/actions-codespell",
    "crate-ci/typos",
    "editorconfig-checker/action-editorconfig-checker",
}
LIGHT_RUN_RE = re.compile(
    r"^\s*(ruff|black|isort|flake8|pylint|mypy|pyright|eslint|prettier|biome|markdownlint(-cli2)?|"
    r"yamllint|actionlint|zizmor|shellcheck|shfmt|hadolint|codespell|typos|pre-commit|"
    r"gh\s+(pr|issue|api|label)|curl|echo|cat|ls|printf|python\s+-m\s+(ruff|black|isort|mypy|flake8)|"
    r"npx\s+(eslint|prettier|markdownlint|commitlint)|npm\s+run\s+(lint|format|prettier)|"
    r"uvx?\s+(run\s+)?(ruff|black|mypy|pre-commit|codespell)|cargo\s+(fmt|clippy)|gofmt|golangci-lint|"
    r"terraform\s+(fmt|validate)|tflint|uv\s+(sync|pip)|pip\s+install|npm\s+(ci|install)|"
    r"export|set\s+-|cd\s|mkdir|git\s)",
    re.IGNORECASE,
)
BROAD_ARTIFACT_PATHS = re.compile(
    r"^(\.|\./|\*|\*\*|\*\*/\*|\./\*\*|build/?|dist/?|target/?|node_modules/?|out/?|\.\*|\$\{\{\s*github\.workspace\s*\}\}/?)$"
)
FULL_HISTORY_NEED_RE = re.compile(
    r"\bgit\s+(log|describe|tag|rev-list|merge-base|shortlog|blame|bisect|cherry|diff\s+\S*\.\.)|"
    r"\b(setuptools[-_]scm|hatch-vcs|versioningit|dunamai|git-cliff|semantic-release|release-please|"
    r"goreleaser|gitversion|changesets|lerna|nx\s+affected|conventional-changelog|standard-version|"
    r"cz\s+bump|commitizen|towncrier|sonar|codeql|gitleaks|trufflehog|git\s+fetch\s+--unshallow)\b",
    re.IGNORECASE,
)
FULL_HISTORY_ACTIONS = {
    "release-drafter/release-drafter",
    "googleapis/release-please-action",
    "goreleaser/goreleaser-action",
    "gittools/actions",
    "changesets/action",
    "orhun/git-cliff-action",
    "cycjimmy/semantic-release-action",
    "sonarsource/sonarcloud-github-action",
    "sonarsource/sonarqube-scan-action",
    "github/codeql-action",
    "gitleaks/gitleaks-action",
    "trufflesecurity/trufflehog",
    "nrwl/nx-set-shas",
    "mikepenz/release-changelog-builder-action",
    "pypa/gh-action-pypi-publish",
    "python-semantic-release/python-semantic-release",
    "codecov/codecov-action",
    "paulhatch/semantic-version",
    "anothrnick/github-tag-action",
    "mathieudutour/github-tag-action",
    "dorny/paths-filter",
    "tj-actions/changed-files",
}


class W002:
    id = "W002"
    title = "Dependency installation with no cache"
    severity = Severity.MEDIUM
    needs_history = False

    def run(self, ctx: Context) -> list[Finding]:
        out: list[Finding] = []
        for wf in ctx.parsed_workflows:
            for job in wf.jobs.values():
                if job.is_reusable_call or job.container or _job_has_cache(job):
                    continue
                install_steps = [s for s in job.steps if _is_install_step(s)]
                if not install_steps:
                    continue
                names = ", ".join(f"`{s.display_name}`" for s in install_steps[:3])
                out.append(
                    advisory(
                        self,
                        wf,
                        job,
                        f"Job `{job.key}` in {wf.path} installs dependencies ({names}) on every run and "
                        "never restores a cache.",
                        Remediation(
                            summary=_cache_suggestion(install_steps),
                            patch={
                                "op": "advise",
                                "job": job.key,
                                "steps": [s.index for s in install_steps],
                            },
                            docs_url=DOCS_URL_CACHE,
                        ),
                        [
                            config_evidence(
                                wf, job, install_steps=[s.display_name for s in install_steps]
                            )
                        ],
                        confidence=Confidence.MEDIUM,
                    )
                )
        return out


def _is_install_step(step: Step) -> bool:
    if step.run and INSTALL_RE.search(step.run):
        return True
    repo = step.action_repo or ""
    return repo in SETUP_WITH_CACHE_INPUT


def _job_has_cache(job: Job) -> bool:
    for s in job.steps:
        repo = s.action_repo or ""
        action = s.action or ""
        if repo in SETUP_WITH_CACHE_INPUT and s.with_.get(SETUP_WITH_CACHE_INPUT[repo]):
            return True
        if repo == "ruby/setup-ruby" and _truthy(s.with_.get("bundler-cache")):
            return True
        if repo == "docker/build-push-action" and s.with_.get("cache-from"):
            return True
        if repo in ("ruby/setup-ruby", "docker/build-push-action"):
            continue
        for cached, off_input in CACHING_ACTIONS.items():
            if cached in (action, repo) or action.startswith(cached + "/"):
                if off_input and str(s.with_.get(off_input, "")).lower() == "false":
                    continue
                return True
        if s.run and re.search(r"\b(cache|restore)\b", s.run, re.I) and "actions/cache" in s.run:
            return True
    return False


def _truthy(v: Any) -> bool:
    return v is True or str(v).strip().lower() == "true"


def _cache_suggestion(steps: list[Step]) -> str:
    text = " ".join((s.run or "") + " " + (s.action_repo or "") for s in steps).lower()
    if "uv" in text:
        return "Use `astral-sh/setup-uv` with `enable-cache: true`, or `actions/cache` on `~/.cache/uv`."
    if "pip" in text or "setup-python" in text:
        return "Set `cache: pip` on `actions/setup-python`, or cache `~/.cache/pip` with `actions/cache`."
    if "poetry" in text:
        return "Cache `~/.cache/pypoetry` and the virtualenv with `actions/cache` keyed on `poetry.lock`."
    if "npm" in text or "yarn" in text or "pnpm" in text or "setup-node" in text:
        return "Set `cache: npm|yarn|pnpm` on `actions/setup-node`."
    if "cargo" in text:
        return "Add `Swatinem/rust-cache` before the build step."
    if "go " in text or "setup-go" in text:
        return "Keep `actions/setup-go` caching enabled (default) or cache `~/go/pkg/mod`."
    if "bundle" in text:
        return "Set `bundler-cache: true` on `ruby/setup-ruby`."
    if "mvn" in text or "gradle" in text or "setup-java" in text:
        return "Set `cache: maven|gradle` on `actions/setup-java`, or use `gradle/actions/setup-gradle`."
    if "apt" in text:
        return "Cache apt packages with `awalsh128/cache-apt-pkgs-action` or prebuild a container image."
    return "Add `actions/cache` keyed on the lockfile for the dependency directory."


class W003:
    id = "W003"
    title = "Job without timeout-minutes (exposure: 360-minute default)"
    severity = Severity.MEDIUM
    needs_history = False

    def run(self, ctx: Context) -> list[Finding]:
        out: list[Finding] = []
        default = ctx.pricing.default_job_timeout_minutes
        for wf in ctx.parsed_workflows:
            if isinstance(wf.raw, dict) and wf.raw.get("timeout-minutes") is not None:
                continue
            for job in wf.jobs.values():
                if job.is_reusable_call or job.timeout_minutes is not None:
                    continue
                out.append(
                    advisory(
                        self,
                        wf,
                        job,
                        f"Job `{job.key}` in {wf.path} has no `timeout-minutes`; a hung step bills up to "
                        f"{default} minutes per leg before GitHub cancels it.",
                        Remediation(
                            summary="Set `timeout-minutes` to roughly 1.5x the job's p95 runtime.",
                            patch={
                                "op": "add_job_key",
                                "job": job.key,
                                "key": "timeout-minutes",
                                "value": 30,
                            },
                            docs_url=DOCS_URL_TIMEOUT,
                        ),
                        [config_evidence(wf, job, default_timeout_minutes=default)],
                        confidence=Confidence.HIGH,
                    )
                )
        return out


class W006:
    id = "W006"
    title = "actions/checkout with fetch-depth: 0 and no observable need for full history"
    severity = Severity.LOW
    needs_history = False

    def run(self, ctx: Context) -> list[Finding]:
        out: list[Finding] = []
        for wf in ctx.parsed_workflows:
            for job in wf.jobs.values():
                deep = [
                    s
                    for s in job.steps
                    if s.action_repo == "actions/checkout"
                    and str(s.with_.get("fetch-depth", "")).strip() == "0"
                ]
                if not deep or _needs_full_history(job):
                    continue
                out.append(
                    advisory(
                        self,
                        wf,
                        job,
                        f"Job `{job.key}` in {wf.path} checks out the full git history "
                        "(`fetch-depth: 0`) but no step in the job appears to use it.",
                        Remediation(
                            summary="Remove `fetch-depth: 0` (default is a shallow clone of depth 1).",
                            patch={
                                "op": "remove_step_with",
                                "job": job.key,
                                "step": deep[0].index,
                                "key": "fetch-depth",
                            },
                            docs_url=DOCS_URL_CHECKOUT,
                        ),
                        [config_evidence(wf, job, step=deep[0].index)],
                        confidence=Confidence.MEDIUM,
                    )
                )
        return out


def _needs_full_history(job: Job) -> bool:
    for s in job.steps:
        if s.action_repo in FULL_HISTORY_ACTIONS:
            return True
        if s.run and FULL_HISTORY_NEED_RE.search(s.run):
            return True
        if s.with_ and any("fetch-tags" in k for k in s.with_):
            return True
    return bool(re.search(r"(release|publish|version|changelog|deploy)", job.key, re.I))


class W007:
    id = "W007"
    title = "Runner larger than the job's work justifies"
    severity = Severity.LOW
    needs_history = False

    def run(self, ctx: Context) -> list[Finding]:
        out: list[Finding] = []
        slim_limit = ctx.pricing.ubuntu_slim_job_timeout_minutes
        for wf in ctx.parsed_workflows:
            for job in wf.jobs.values():
                if job.is_reusable_call or job.container or job.services or not job.steps:
                    continue
                if job.runs_on_has_expression or len(job.runs_on) != 1:
                    continue
                label = job.runs_on[0].lower()
                if not re.fullmatch(r"ubuntu-(latest|\d\d\.\d\d)", label):
                    continue
                if not all(_is_light_step(s) for s in job.steps):
                    continue
                out.append(
                    advisory(
                        self,
                        wf,
                        job,
                        f"Job `{job.key}` in {wf.path} runs on `{label}` (2-core, list price "
                        f"${ctx.model.sku('actions_linux').per_minute}/min) but every step is lightweight "
                        "(linting, labelling, scripting). `ubuntu-slim` (1 vCPU, "
                        f"${ctx.model.sku('actions_linux_slim').per_minute}/min, {slim_limit}-minute job limit) "
                        "is built for this.",
                        Remediation(
                            summary="Switch `runs-on` to `ubuntu-slim` if the job stays well under 15 minutes.",
                            patch={
                                "op": "set_job_key",
                                "job": job.key,
                                "key": "runs-on",
                                "value": "ubuntu-slim",
                                "safe": False,  # 15-minute job limit: only applied with --rules W007
                            },
                            docs_url=DOCS_URL_SLIM,
                        ),
                        [
                            config_evidence(
                                wf, job, runs_on=label, steps=[s.display_name for s in job.steps]
                            )
                        ],
                        confidence=Confidence.LOW,
                    )
                )
        return out


def _is_light_step(step: Step) -> bool:
    if step.uses:
        repo = step.action_repo or ""
        if step.uses.startswith("./"):
            return False
        return repo in LIGHT_ACTIONS
    if step.run:
        lines = [
            ln for ln in step.run.splitlines() if ln.strip() and not ln.strip().startswith("#")
        ]
        return all(LIGHT_RUN_RE.match(ln) for ln in lines) if lines else True
    return True


class W011:
    id = "W011"
    title = "Artifact upload of a broad path at default retention"
    severity = Severity.LOW
    needs_history = False

    def run(self, ctx: Context) -> list[Finding]:
        out: list[Finding] = []
        for wf in ctx.parsed_workflows:
            for job in wf.jobs.values():
                for s in job.steps:
                    if s.action_repo != "actions/upload-artifact":
                        continue
                    path = str(s.with_.get("path", "")).strip()
                    broad = any(
                        BROAD_ARTIFACT_PATHS.match(p.strip())
                        for p in path.splitlines()
                        if p.strip()
                    )
                    if not broad or s.with_.get("retention-days") is not None:
                        continue
                    out.append(
                        advisory(
                            self,
                            wf,
                            job,
                            f"Job `{job.key}` in {wf.path} uploads `{path}` as an artifact with the default "
                            "retention (up to 90 days); broad paths accrue storage charges long after the "
                            "run is forgotten.",
                            Remediation(
                                summary="Narrow `path` to what is needed and set `retention-days` (e.g. 7).",
                                patch={
                                    "op": "add_step_with",
                                    "job": job.key,
                                    "step": s.index,
                                    "key": "retention-days",
                                    "value": 7,
                                },
                                docs_url=DOCS_URL_ARTIFACT,
                            ),
                            [config_evidence(wf, job, step=s.index, path=path)],
                            confidence=Confidence.MEDIUM,
                            tags=["storage-not-minutes"],
                        )
                    )
        return out


__all__ = ["W002", "W003", "W006", "W007", "W011", "Workflow"]
