"""Top-level static analysis entry point."""

from __future__ import annotations

from pathlib import Path

from ciburn.findings import Finding, sort_findings
from ciburn.pricing import DEFAULT_MODEL, Pricing
from ciburn.repo_layout import RepoLayout
from ciburn.rules import STATIC_RULES, Context, run_rules
from ciburn.workflow import Workflow, load_workflows


def analyze_static(
    repo_root: Path,
    pricing: Pricing | None = None,
    model_id: str = DEFAULT_MODEL,
    layout: RepoLayout | None = None,
) -> tuple[list[Workflow], list[Finding]]:
    pricing = pricing or Pricing.load()
    workflows = load_workflows(repo_root)
    ctx = Context(
        workflows=workflows,
        pricing=pricing,
        model=pricing.model(model_id),
        layout=layout if layout is not None else RepoLayout.from_path(repo_root),
    )
    return workflows, sort_findings(run_rules(ctx, STATIC_RULES))
