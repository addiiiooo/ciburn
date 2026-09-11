"""Parse GitHub Actions workflow files into a small typed model.

Only what the rules need is modelled. Anything expression-valued
(``${{ ... }}``) is kept as text and treated as "unknown" rather than guessed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

EXPR_RE = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)
WORKFLOW_GLOBS = ("*.yml", "*.yaml")


class WorkflowParseError(ValueError):
    pass


@dataclass
class Step:
    index: int
    name: str | None
    uses: str | None
    run: str | None
    with_: dict[str, Any]
    if_: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def action(self) -> str | None:
        """``owner/repo`` (lowercase, no version) for ``uses`` steps."""
        if not self.uses:
            return None
        base = self.uses.split("@", 1)[0].strip()
        if base.startswith("./") or base.startswith("docker://"):
            return base.lower()
        parts = base.split("/")
        if len(parts) >= 2:
            return "/".join(parts[:2]).lower() + (
                "/" + "/".join(parts[2:]) if len(parts) > 2 else ""
            )
        return base.lower()

    @property
    def action_repo(self) -> str | None:
        a = self.action
        if a is None:
            return None
        parts = a.split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else a

    @property
    def display_name(self) -> str:
        if self.name:
            return self.name
        if self.run:
            return "Run " + self.run.strip().splitlines()[0][:80]
        if self.uses:
            return "Run " + self.uses
        return f"step {self.index}"


@dataclass
class Job:
    key: str
    name: str | None
    runs_on: list[str]
    runs_on_raw: Any
    timeout_minutes: int | None  # None = absent; -1 = expression (unknown)
    steps: list[Step]
    needs: list[str]
    if_: str | None
    uses: str | None
    matrix: dict[str, Any] | None
    matrix_legs: int | None  # None = unknown (expression) or no matrix
    fail_fast: bool | None
    continue_on_error: Any
    concurrency: Any
    services: dict[str, Any]
    container: Any
    line: int | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def is_reusable_call(self) -> bool:
        return self.uses is not None

    @property
    def runs_on_has_expression(self) -> bool:
        return any(EXPR_RE.search(lb) for lb in self.runs_on)

    def api_name_matches(self, api_name: str) -> bool:
        """Does a job name reported by the Actions API belong to this config job?"""
        api_name = api_name.strip()
        if self.name is None or not self.name.strip():
            return api_name == self.key or api_name.startswith(self.key + " (")
        if EXPR_RE.search(self.name):
            pattern = "^" + ".*?".join(re.escape(p) for p in EXPR_RE.split(self.name)) + "$"
            return re.match(pattern, api_name, re.DOTALL) is not None
        return api_name == self.name or api_name.startswith(self.name + " (")

    def display(self) -> str:
        return self.key


@dataclass
class Workflow:
    path: str
    name: str | None
    on: dict[str, dict[str, Any]]
    concurrency: Any
    jobs: dict[str, Job]
    source: str = field(repr=False, default="")
    parse_error: str | None = None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)
    env: dict[str, Any] = field(default_factory=dict)

    @property
    def events(self) -> list[str]:
        return list(self.on)

    def has_event(self, *names: str) -> bool:
        return any(n in self.on for n in names)

    @property
    def crons(self) -> list[str]:
        sched = self.on.get("schedule")
        if not sched:
            return []
        items = sched.get("_list", [])
        out: list[str] = []
        for it in items:
            if isinstance(it, dict) and "cron" in it:
                out.append(str(it["cron"]))
        return out

    @property
    def is_reusable(self) -> bool:
        return "workflow_call" in self.on

    def job_for_api_name(self, api_name: str) -> Job | None:
        # exact key/name match first, then prefix match (matrix legs)
        for job in self.jobs.values():
            if api_name == job.key or (job.name and api_name == job.name):
                return job
        for job in self.jobs.values():
            if job.api_name_matches(api_name):
                return job
        return None


# --- loading --------------------------------------------------------------------


def workflow_files(repo_root: Path) -> list[Path]:
    wf_dir = repo_root / ".github" / "workflows"
    if not wf_dir.is_dir():
        return []
    files: list[Path] = []
    for pattern in WORKFLOW_GLOBS:
        files.extend(p for p in wf_dir.glob(pattern) if p.is_file())
    return sorted(set(files))


def load_workflows(repo_root: Path) -> list[Workflow]:
    out: list[Workflow] = []
    for p in workflow_files(repo_root):
        rel = p.relative_to(repo_root).as_posix()
        text = p.read_text(encoding="utf-8", errors="replace")
        out.append(parse_workflow(text, rel))
    return out


def load_workflow_texts(items: Iterable[tuple[str, str]]) -> list[Workflow]:
    """Parse ``(path, text)`` pairs, e.g. fetched from the API."""
    return [parse_workflow(text, path) for path, text in items]


def parse_workflow(text: str, path: str) -> Workflow:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return Workflow(
            path=path,
            name=None,
            on={},
            concurrency=None,
            jobs={},
            source=text,
            parse_error=f"YAML error: {exc}",
        )
    if not isinstance(data, dict):
        return Workflow(
            path=path,
            name=None,
            on={},
            concurrency=None,
            jobs={},
            source=text,
            parse_error="workflow is not a mapping",
        )
    # PyYAML (YAML 1.1) reads the bare key `on` as boolean True.
    on_raw = data.get("on", data.get(True))
    jobs_raw = data.get("jobs")
    if not isinstance(jobs_raw, dict):
        jobs_raw = {}
    lines = _job_lines(text)
    jobs = {
        str(k): _parse_job(str(k), v if isinstance(v, dict) else {}, lines.get(str(k)))
        for k, v in jobs_raw.items()
    }
    return Workflow(
        path=path,
        name=str(data["name"]) if data.get("name") is not None else None,
        on=_normalize_on(on_raw),
        concurrency=data.get("concurrency"),
        jobs=jobs,
        source=text,
        raw=data,
        env=_as_dict(data.get("env")),
    )


def _normalize_on(on_raw: Any) -> dict[str, dict[str, Any]]:
    if on_raw is None:
        return {}
    if isinstance(on_raw, str):
        return {on_raw: {}}
    if isinstance(on_raw, list):
        return {str(e): {} for e in on_raw}
    if isinstance(on_raw, dict):
        out: dict[str, dict[str, Any]] = {}
        for k, v in on_raw.items():
            if isinstance(v, dict):
                out[str(k)] = v
            elif isinstance(v, list):
                out[str(k)] = {"_list": v}
            else:
                out[str(k)] = {}
        return out
    return {}


def _parse_job(key: str, raw: dict[str, Any], line: int | None) -> Job:
    strategy = raw.get("strategy") if isinstance(raw.get("strategy"), dict) else {}
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
    matrix_dict = matrix if isinstance(matrix, dict) else None
    legs = matrix_leg_count(matrix)
    runs_on_raw = raw.get("runs-on")
    runs_on = _runs_on_labels(runs_on_raw, matrix_dict)
    steps_raw = _as_list(raw.get("steps"))
    steps = [
        Step(
            index=i,
            name=str(s.get("name")) if isinstance(s, dict) and s.get("name") is not None else None,
            uses=str(s.get("uses")) if isinstance(s, dict) and s.get("uses") else None,
            run=str(s.get("run")) if isinstance(s, dict) and s.get("run") is not None else None,
            with_=_as_dict(s.get("with")) if isinstance(s, dict) else {},
            if_=str(s.get("if")) if isinstance(s, dict) and s.get("if") is not None else None,
            raw=s if isinstance(s, dict) else {},
        )
        for i, s in enumerate(steps_raw)
    ]
    needs_raw = raw.get("needs")
    needs = (
        [str(n) for n in needs_raw]
        if isinstance(needs_raw, list)
        else ([str(needs_raw)] if isinstance(needs_raw, str) else [])
    )
    fail_fast_raw = strategy.get("fail-fast") if isinstance(strategy, dict) else None
    fail_fast: bool | None
    if isinstance(fail_fast_raw, bool):
        fail_fast = fail_fast_raw
    elif fail_fast_raw is None:
        fail_fast = None
    else:
        fail_fast = None  # expression: unknown
    return Job(
        key=key,
        name=str(raw["name"]) if raw.get("name") is not None else None,
        runs_on=runs_on,
        runs_on_raw=runs_on_raw,
        timeout_minutes=_timeout(raw.get("timeout-minutes")),
        steps=steps,
        needs=needs,
        if_=str(raw["if"]) if raw.get("if") is not None else None,
        uses=str(raw["uses"]) if raw.get("uses") else None,
        matrix=matrix_dict,
        matrix_legs=legs,
        fail_fast=fail_fast,
        continue_on_error=raw.get("continue-on-error"),
        concurrency=raw.get("concurrency"),
        services=_as_dict(raw.get("services")),
        container=raw.get("container"),
        line=line,
        raw=raw,
    )


def _as_dict(v: Any) -> dict[str, Any]:
    return {str(k): val for k, val in v.items()} if isinstance(v, dict) else {}


def _as_list(v: Any) -> list[Any]:
    return list(v) if isinstance(v, list) else []


def _timeout(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        return int(v)
    if isinstance(v, str):
        s = v.strip()
        if s.isdigit():
            return int(s)
        return -1
    return -1


def _runs_on_labels(runs_on: Any, matrix: dict[str, Any] | None) -> list[str]:
    if runs_on is None:
        return []
    if isinstance(runs_on, dict):  # {group:, labels:}
        labels = runs_on.get("labels", [])
        if isinstance(labels, str):
            labels = [labels]
        return [str(lb) for lb in labels] if isinstance(labels, list) else []
    if isinstance(runs_on, list):
        out: list[str] = []
        for lb in runs_on:
            out.extend(_runs_on_labels(lb, matrix))
        return out
    s = str(runs_on).strip()
    m = re.fullmatch(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}", s)
    if m and matrix:
        vals = matrix.get(m.group(1))
        if isinstance(vals, list):
            return [str(v) for v in vals if isinstance(v, str | int | float)]
    return [s]


def matrix_leg_count(matrix: Any) -> int | None:
    """Number of jobs a matrix expands to; None when an expression makes it unknown."""
    if matrix is None:
        return None
    if not isinstance(matrix, dict):
        return None  # ${{ fromJson(...) }}
    product = 1
    have_axis = False
    for k, v in matrix.items():
        if k in ("include", "exclude"):
            continue
        if isinstance(v, list):
            product *= max(len(v), 1)
            have_axis = True
        else:
            return None  # expression-valued axis
    include = matrix.get("include")
    exclude = matrix.get("exclude")
    if isinstance(include, str) or isinstance(exclude, str):
        return None
    inc = len(include) if isinstance(include, list) else 0
    exc = len(exclude) if isinstance(exclude, list) else 0
    if not have_axis:
        return inc or None
    # include entries that add new keys extend existing legs rather than adding legs;
    # we can't tell without evaluating, so count them as extra legs only when they
    # introduce a combination not already covered. Approximation: treat as additions.
    return max(product - exc + inc, 0)


def _job_lines(text: str) -> dict[str, int]:
    """Map job key -> 1-based line number, via the YAML node graph."""
    try:
        node = yaml.compose(text)
    except yaml.YAMLError:
        return {}
    if not isinstance(node, yaml.MappingNode):
        return {}
    for k, v in node.value:
        if isinstance(k, yaml.ScalarNode) and k.value == "jobs" and isinstance(v, yaml.MappingNode):
            return {
                str(jk.value): jk.start_mark.line + 1
                for jk, _ in v.value
                if isinstance(jk, yaml.ScalarNode)
            }
    return {}


def iter_steps(workflows: Iterable[Workflow]) -> Iterator[tuple[Workflow, Job, Step]]:
    for wf in workflows:
        for job in wf.jobs.values():
            for step in job.steps:
                yield wf, job, step
