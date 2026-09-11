"""`ciburn fix`: turn machine-readable remediations into a unified diff.

Text-based editing on purpose: a YAML round-trip would reformat the whole file
and produce an unreviewable patch. Every edit is verified by re-parsing the
result and checking the intended key landed where it should; an edit that does
not verify is dropped and reported. Never commits, never pushes.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ciburn.findings import Finding

SUPPORTED_OPS = {
    "add_workflow_key",
    "add_job_key",
    "set_job_key",
    "add_trigger_key",
    "set_strategy_key",
}


@dataclass
class FixOutcome:
    applied: list[tuple[Finding, str]] = field(default_factory=list)  # (finding, description)
    skipped: list[tuple[Finding, str]] = field(default_factory=list)  # (finding, reason)
    diffs: dict[str, str] = field(default_factory=dict)  # path -> unified diff
    new_texts: dict[str, str] = field(default_factory=dict)

    @property
    def patch(self) -> str:
        return "".join(self.diffs[p] for p in sorted(self.diffs))


def plan_fixes(
    findings: list[Finding], repo_root: Path, rules: set[str] | None = None
) -> FixOutcome:
    out = FixOutcome()
    texts: dict[str, str] = {}
    originals: dict[str, str] = {}
    for f in findings:
        if rules and f.rule_id not in rules:
            continue
        op = str(f.remediation.patch.get("op", ""))
        if op not in SUPPORTED_OPS or not f.workflow:
            out.skipped.append((f, f"no automatic fix for {f.rule_id} ({op or 'advice only'})"))
            continue
        if not f.remediation.patch.get("safe", True) and not (rules and f.rule_id in rules):
            out.skipped.append(
                (f, f"{f.rule_id} changes behaviour; apply it explicitly with --rules {f.rule_id}")
            )
            continue
        path = repo_root / f.workflow
        if f.workflow not in texts:
            if not path.is_file():
                out.skipped.append((f, f"{f.workflow} not found under {repo_root}"))
                continue
            originals[f.workflow] = texts[f.workflow] = path.read_text(encoding="utf-8")
        try:
            new_text, desc = apply_patch(texts[f.workflow], f.remediation.patch)
        except FixError as exc:
            out.skipped.append((f, str(exc)))
            continue
        texts[f.workflow] = new_text
        out.applied.append((f, desc))
    for wf, text in texts.items():
        if text != originals[wf]:
            out.new_texts[wf] = text
            out.diffs[wf] = "".join(
                difflib.unified_diff(
                    originals[wf].splitlines(keepends=True),
                    text.splitlines(keepends=True),
                    fromfile=f"a/{wf}",
                    tofile=f"b/{wf}",
                )
            )
    return out


class FixError(ValueError):
    pass


def apply_patch(text: str, patch: dict[str, Any]) -> tuple[str, str]:
    op = patch["op"]
    if op == "add_workflow_key":
        return _add_workflow_key(text, str(patch["key"]), patch["value"])
    if op in ("add_job_key", "set_job_key"):
        return _set_job_key(
            text, str(patch["job"]), str(patch["key"]), patch["value"], replace=op == "set_job_key"
        )
    if op == "set_strategy_key":
        return _set_strategy_key(text, str(patch["job"]), str(patch["key"]), patch["value"])
    if op == "add_trigger_key":
        return _add_trigger_key(
            text, [str(e) for e in patch["events"]], str(patch["key"]), patch["value"]
        )
    raise FixError(f"unsupported op {op}")


def _dump(value: Any, indent: int) -> list[str]:
    """YAML-render ``value`` as block lines indented by ``indent`` spaces."""
    if isinstance(value, dict | list):
        body = yaml.safe_dump(
            value, default_flow_style=False, sort_keys=False, allow_unicode=True
        ).rstrip("\n")
        return [" " * indent + ln if ln else ln for ln in body.splitlines()]
    return [" " * indent + _scalar(value)]


def _scalar(value: Any) -> str:
    return yaml.safe_dump(value, default_flow_style=True).strip().removesuffix("\n...").strip()


def _on_block(data: dict[Any, Any]) -> Any:
    """The `on:` value; PyYAML reads a bare `on` key as boolean True."""
    return data["on"] if "on" in data else data.get(True)


def _reparse(text: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise FixError(f"edit produced invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise FixError("edit produced a non-mapping document")
    return data


def _add_workflow_key(text: str, key: str, value: Any) -> tuple[str, str]:
    data = _reparse(text)
    if key in data:
        raise FixError(f"workflow already has `{key}`")
    lines = text.splitlines(keepends=True)
    idx = next((i for i, ln in enumerate(lines) if re.match(r"^jobs\s*:", ln)), None)
    if idx is None:
        raise FixError("could not find top-level `jobs:`")
    block = [f"{key}:\n"] + [ln + "\n" for ln in _dump(value, 2)] + ["\n"]
    new = "".join(lines[:idx] + block + lines[idx:])
    if _reparse(new).get(key) != value:
        raise FixError(f"verification failed after inserting `{key}`")
    return new, f"added top-level `{key}`"


def _job_span(lines: list[str], job: str) -> tuple[int, int, int]:
    """(start, end, indent) line indices of a job block; end is exclusive."""
    jobs_idx = next((i for i, ln in enumerate(lines) if re.match(r"^jobs\s*:", ln)), None)
    if jobs_idx is None:
        raise FixError("could not find top-level `jobs:`")
    start = None
    indent = 0
    for i in range(jobs_idx + 1, len(lines)):
        m = re.match(r"^(\s+)(['\"]?)" + re.escape(job) + r"\2\s*:", lines[i])
        if m:
            start, indent = i, len(m.group(1))
            break
        if lines[i].strip() and not lines[i].startswith((" ", "\t", "#")):
            break
    if start is None:
        raise FixError(f"could not find job `{job}`")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if ln.strip() and not ln.startswith("#") and (len(ln) - len(ln.lstrip(" "))) <= indent:
            end = i
            break
    return start, end, indent


def _set_job_key(text: str, job: str, key: str, value: Any, *, replace: bool) -> tuple[str, str]:
    lines = text.splitlines(keepends=True)
    start, end, indent = _job_span(lines, job)
    child = indent + 2
    key_re = re.compile(r"^" + " " * child + re.escape(key) + r"\s*:")
    existing = next((i for i in range(start + 1, end) if key_re.match(lines[i])), None)
    rendered = [ln + "\n" for ln in _dump(value, child)]
    if isinstance(value, dict | list):
        block = [" " * child + f"{key}:\n"] + [ln + "\n" for ln in _dump(value, child + 2)]
    else:
        block = [" " * child + f"{key}: {_scalar(value)}\n"]
    _ = rendered
    if existing is not None:
        if not replace:
            raise FixError(f"job `{job}` already has `{key}`")
        # remove the existing key and any more-indented continuation lines
        stop = existing + 1
        while stop < end and (
            not lines[stop].strip() or (len(lines[stop]) - len(lines[stop].lstrip(" "))) > child
        ):
            stop += 1
        new_lines = lines[:existing] + block + lines[stop:]
    else:
        anchor = next(
            (
                i
                for i in range(start + 1, end)
                if re.match(r"^" + " " * child + r"runs-on\s*:", lines[i])
            ),
            None,
        )
        if anchor is None:
            insert_at = start + 1
        else:
            insert_at = anchor + 1
            while (
                insert_at < end
                and (len(lines[insert_at]) - len(lines[insert_at].lstrip(" "))) > child
                and lines[insert_at].strip()
            ):
                insert_at += 1
        new_lines = lines[:insert_at] + block + lines[insert_at:]
    new = "".join(new_lines)
    got = _reparse(new).get("jobs", {}).get(job, {}).get(key)
    if got != value:
        raise FixError(f"verification failed: jobs.{job}.{key} is {got!r}, expected {value!r}")
    return new, f"set `jobs.{job}.{key}: {_scalar(value)}`"


def _set_strategy_key(text: str, job: str, key: str, value: Any) -> tuple[str, str]:
    lines = text.splitlines(keepends=True)
    start, end, indent = _job_span(lines, job)
    child = indent + 2
    strat = next(
        (
            i
            for i in range(start + 1, end)
            if re.match(r"^" + " " * child + r"strategy\s*:", lines[i])
        ),
        None,
    )
    if strat is None:
        raise FixError(f"job `{job}` has no `strategy:` block")
    key_re = re.compile(r"^" + " " * (child + 2) + re.escape(key) + r"\s*:")
    existing = next((i for i in range(strat + 1, end) if key_re.match(lines[i])), None)
    block = [" " * (child + 2) + f"{key}: {_scalar(value)}\n"]
    if existing is not None:
        new_lines = lines[:existing] + block + lines[existing + 1 :]
    else:
        new_lines = lines[: strat + 1] + block + lines[strat + 1 :]
    new = "".join(new_lines)
    got = _reparse(new).get("jobs", {}).get(job, {}).get("strategy", {}).get(key)
    if got != value:
        raise FixError(f"verification failed: jobs.{job}.strategy.{key} is {got!r}")
    return new, f"set `jobs.{job}.strategy.{key}: {_scalar(value)}`"


def _add_trigger_key(text: str, events: list[str], key: str, value: Any) -> tuple[str, str]:
    data = _reparse(text)
    on_raw = _on_block(data)
    if not isinstance(on_raw, dict):
        raise FixError("`on:` is not a mapping; rewrite it as a mapping first")
    lines = text.splitlines(keepends=True)
    on_idx = next((i for i, ln in enumerate(lines) if re.match(r"^on\s*:", ln)), None)
    if on_idx is None:
        raise FixError("could not find top-level `on:`")
    done: list[str] = []
    for ev in events:
        cfg = on_raw.get(ev)
        if isinstance(cfg, dict) and key in cfg:
            continue
        ev_idx = None
        ev_indent = 0
        for i in range(on_idx + 1, len(lines)):
            ln = lines[i]
            if ln.strip() and not ln.startswith((" ", "\t", "#")):
                break
            m = re.match(r"^(\s+)" + re.escape(ev) + r"\s*:\s*(#.*)?$", ln)
            if m:
                ev_idx, ev_indent = i, len(m.group(1))
                break
        if ev_idx is None:
            raise FixError(f"could not find `{ev}:` as a block under `on:`")
        block = [" " * (ev_indent + 2) + f"{key}:\n"] + [
            ln + "\n" for ln in _dump(value, ev_indent + 4)
        ]
        lines = lines[: ev_idx + 1] + block + lines[ev_idx + 1 :]
        done.append(ev)
    if not done:
        raise FixError(f"every listed event already has `{key}`")
    new = "".join(lines)
    new_on = _on_block(_reparse(new))
    for ev in done:
        if (
            not isinstance(new_on, dict)
            or not isinstance(new_on.get(ev), dict)
            or new_on[ev].get(key) != value
        ):
            raise FixError(f"verification failed for on.{ev}.{key}")
    return new, f"added `{key}` under on.{', on.'.join(done)}"
