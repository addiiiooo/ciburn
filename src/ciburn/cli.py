"""ciburn command-line interface."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import click
from rich.console import Console

from ciburn import __version__
from ciburn.audit import AuditOptions, AuditResult, parse_label_overrides, run_audit
from ciburn.findings import Severity
from ciburn.pricing import DEFAULT_MODEL, Pricing
from ciburn.report import FORMATS, render_html, render_json, render_markdown, render_terminal

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2
FAIL_ON = ("none", "low", "medium", "high")


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, "--version", "-V", prog_name="ciburn")
def cli() -> None:
    """ciburn: what your GitHub Actions CI costs, and where the money goes to waste.

    Works offline against a checkout (static rules) and, with a repository name or a
    GitHub remote, joins the repository's own run history to every finding.
    Zero LLM calls, no telemetry, no service.
    """


def _common_options(fn: Any) -> Any:
    opts = [
        click.option(
            "--repo",
            "-r",
            help="owner/name on github.com (default: detected from the git remote of --path)",
        ),
        click.option(
            "--path",
            "-p",
            type=click.Path(path_type=Path),
            help="local checkout to read .github/workflows from",
        ),
        click.option(
            "--days",
            "-d",
            type=click.IntRange(1, 365),
            default=90,
            show_default=True,
            help="history window",
        ),
        click.option(
            "--model",
            "-m",
            "model_id",
            default=DEFAULT_MODEL,
            show_default=True,
            help="pricing model id (see `ciburn models`)",
        ),
        click.option(
            "--plan",
            type=click.Choice(["free", "pro", "free_org", "team", "enterprise"]),
            help="apply this plan's included minutes (labelled assumption)",
        ),
        click.option(
            "--cache",
            type=click.Path(path_type=Path),
            help="SQLite cache path (default: $CIBURN_CACHE or ~/.cache/ciburn/history.sqlite)",
        ),
        click.option(
            "--token",
            envvar=["GITHUB_TOKEN", "GH_TOKEN"],
            help="GitHub token (env GITHUB_TOKEN/GH_TOKEN)",
        ),
        click.option(
            "--label-sku",
            multiple=True,
            help="map a custom runner label to a SKU, e.g. ubuntu-16core=linux_16_core",
        ),
        click.option("--offline", is_flag=True, help="use cached history only; no network"),
        click.option(
            "--max-job-runs", type=int, help="fetch job data for at most N runs (sampling)"
        ),
        click.option(
            "--no-commits",
            is_flag=True,
            help="skip fetching commit file lists (W004 stays advisory)",
        ),
        click.option("--quiet", "-q", is_flag=True, help="no progress output on stderr"),
    ]
    for o in reversed(opts):
        fn = o(fn)
    return fn


def _progress(quiet: bool) -> Any:
    err = Console(stderr=True)

    def cb(event: str, info: dict[str, Any]) -> None:
        if quiet:
            return
        if event == "repo":
            err.print(f"[dim]fetching history for {info['full_name']}…[/dim]")
        elif event == "runs":
            err.print(f"[dim]  runs: {info['seen']}[/dim]", end="\r")
        elif event == "jobs":
            err.print(
                f"[dim]  jobs: run {info['done']}/{info['total']} ({info['jobs']} jobs)[/dim]",
                end="\r",
            )
        elif event == "commits":
            err.print(f"[dim]  commits: {info['done']}/{info['total']}[/dim]", end="\r")
        elif event == "log":
            err.print(f"[yellow]{info['message']}[/yellow]")
        elif event == "done":
            err.print(f"[dim]  done ({info['requests']} API requests)[/dim]")

    return cb


def _build_options(
    repo: str | None,
    path: Path | None,
    days: int,
    model_id: str,
    plan: str | None,
    cache: Path | None,
    token: str | None,
    label_sku: tuple[str, ...],
    offline: bool,
    max_job_runs: int | None,
    no_commits: bool,
    quiet: bool,
    no_history: bool = False,
) -> AuditOptions:
    if path is None and repo is None:
        path = Path.cwd()
    if path is not None and not (path / ".github" / "workflows").is_dir() and repo is None:
        raise click.UsageError(f"{path} has no .github/workflows directory and no --repo was given")
    try:
        overrides = parse_label_overrides(label_sku)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    return AuditOptions(
        path=path,
        repo=repo,
        days=days,
        model_id=model_id,
        plan=plan,
        cache=cache,
        token=token,
        no_history=no_history,
        offline=offline,
        label_overrides=overrides,
        max_job_runs=max_job_runs,
        fetch_commits=not no_commits,
        progress=_progress(quiet),
    )


def _emit(result: AuditResult, fmt: str, output: Path | None) -> None:
    if fmt == "terminal":
        if output:
            with output.open("w", encoding="utf-8") as fh:
                render_terminal(result, Console(file=fh, width=120))
        else:
            render_terminal(result, Console())
        return
    text = {"md": render_markdown, "json": render_json, "html": render_html}[fmt](result)
    if output:
        output.write_text(text, encoding="utf-8")
    else:
        click.echo(text, nl=False)


def _exit_code(result: AuditResult, fail_on: str) -> int:
    if result.error:
        return EXIT_ERROR
    if fail_on == "none":
        return EXIT_CLEAN
    threshold = Severity(fail_on).rank
    worst = result.worst_severity(measured_only=False)
    if worst is not None and worst.rank >= threshold:
        return EXIT_FINDINGS
    return EXIT_CLEAN


@cli.command()
@_common_options
@click.option(
    "--format", "-f", "fmt", type=click.Choice(FORMATS), default="terminal", show_default=True
)
@click.option(
    "--output", "-o", type=click.Path(path_type=Path), help="write the report to this file"
)
@click.option(
    "--fail-on",
    type=click.Choice(FAIL_ON),
    default="none",
    show_default=True,
    help="exit 1 when any finding has at least this severity (CI gate)",
)
@click.option("--no-history", is_flag=True, help="static analysis only; never touch the network")
def audit(
    repo: str | None,
    path: Path | None,
    days: int,
    model_id: str,
    plan: str | None,
    cache: Path | None,
    token: str | None,
    label_sku: tuple[str, ...],
    offline: bool,
    max_job_runs: int | None,
    no_commits: bool,
    quiet: bool,
    fmt: str,
    output: Path | None,
    fail_on: str,
    no_history: bool,
) -> None:
    """Analyse workflows and run history; report where CI minutes and money go.

    Exit codes: 0 clean, 1 findings at or above --fail-on, 2 error.
    """
    try:
        opts = _build_options(
            repo,
            path,
            days,
            model_id,
            plan,
            cache,
            token,
            label_sku,
            offline,
            max_job_runs,
            no_commits,
            quiet,
            no_history,
        )
        result = run_audit(opts)
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(EXIT_ERROR)
    _emit(result, fmt, output)
    if result.error:
        click.echo(f"error: {result.error}", err=True)
    sys.exit(_exit_code(result, fail_on))


@cli.command()
@_common_options
@click.option("--compare", is_flag=True, help="price the same history under every model")
@click.option(
    "--format",
    "-f",
    "fmt",
    type=click.Choice(["terminal", "json"]),
    default="terminal",
    show_default=True,
)
def price(
    repo: str | None,
    path: Path | None,
    days: int,
    model_id: str,
    plan: str | None,
    cache: Path | None,
    token: str | None,
    label_sku: tuple[str, ...],
    offline: bool,
    max_job_runs: int | None,
    no_commits: bool,
    quiet: bool,
    compare: bool,
    fmt: str,
) -> None:
    """Price the observed history under one pricing model, or compare all of them."""
    from ciburn.pricecmd import run_price

    try:
        opts = _build_options(
            repo,
            path,
            days,
            model_id,
            plan,
            cache,
            token,
            label_sku,
            offline,
            max_job_runs,
            no_commits,
            quiet,
        )
        code = run_price(opts, compare=compare, fmt=fmt)
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(EXIT_ERROR)
    sys.exit(code)


@cli.command()
def rules() -> None:
    """List every rule with its severity and whether it needs run history."""
    from ciburn.rules import ALL_RULES

    for r in ALL_RULES:
        kind = "history" if r.needs_history else "static "
        click.echo(f"{r.id}  {kind}  {r.severity.value:6}  {r.title}")


@cli.command()
def models() -> None:
    """List the pricing models in pricing.yaml with their sources."""
    p = Pricing.load()
    click.echo(f"pricing.yaml fetched {p.fetched_at:%Y-%m-%d} ({p.age().days} days ago)")
    for m in p.models.values():
        flag = "in effect" if m.in_effect else m.status
        linux = m.sku("actions_linux").per_minute
        mac = m.sku("actions_macos").per_minute
        click.echo(
            f"  {m.id:26} {flag:10} linux ${linux}/min  macos ${mac}/min  self-hosted ${m.self_hosted_per_minute}/min"
        )
        click.echo(f"  {'':26} {m.source_url}")


def main() -> None:
    cli(prog_name="ciburn")


if __name__ == "__main__":  # pragma: no cover
    main()


@cli.command()
@_common_options
@click.option("--rules", "rule_ids", help="comma-separated rule ids to fix (default: all fixable)")
@click.option(
    "--write",
    is_flag=True,
    help="modify the workflow files in place (default: print a unified diff)",
)
@click.option(
    "--dry-run", is_flag=True, help="print the diff only (the default; kept for explicitness)"
)
@click.option(
    "--output", "-o", type=click.Path(path_type=Path), help="write the unified diff to this file"
)
@click.option(
    "--no-history", is_flag=True, help="do not use run history to refine values (e.g. timeouts)"
)
def fix(
    repo: str | None,
    path: Path | None,
    days: int,
    model_id: str,
    plan: str | None,
    cache: Path | None,
    token: str | None,
    label_sku: tuple[str, ...],
    offline: bool,
    max_job_runs: int | None,
    no_commits: bool,
    quiet: bool,
    rule_ids: str | None,
    write: bool,
    dry_run: bool,
    output: Path | None,
    no_history: bool,
) -> None:
    """Write a patch for fixable findings (W001, W003, W004, W007, W009, W012, H005, H007).

    Prints a unified diff you can review and `git apply`. Never commits, never pushes,
    never opens a pull request.
    """
    from ciburn.fix import plan_fixes

    if path is None:
        path = Path.cwd()
    try:
        opts = _build_options(
            repo,
            path,
            days,
            model_id,
            plan,
            cache,
            token,
            label_sku,
            offline,
            max_job_runs,
            no_commits,
            quiet,
            no_history,
        )
        result = run_audit(opts)
        wanted = {r.strip().upper() for r in rule_ids.split(",")} if rule_ids else None
        outcome = plan_fixes(result.findings, path, wanted)
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(EXIT_ERROR)
    for f, reason in outcome.skipped:
        if wanted is None and reason.startswith("no automatic fix"):
            continue
        click.echo(f"skip {f.rule_id} {f.location}: {reason}", err=True)
    for f, desc in outcome.applied:
        click.echo(f"fix  {f.rule_id} {f.location}: {desc}", err=True)
    if not outcome.diffs:
        click.echo("nothing to fix", err=True)
        sys.exit(EXIT_CLEAN)
    if output:
        output.write_text(outcome.patch, encoding="utf-8")
        click.echo(f"patch written to {output}", err=True)
    elif not write:
        click.echo(outcome.patch, nl=False)
    if write and not dry_run:
        for wf, text in outcome.new_texts.items():
            (path / wf).write_text(text, encoding="utf-8")
        click.echo(f"wrote {len(outcome.new_texts)} file(s); review with `git diff`", err=True)
    sys.exit(EXIT_CLEAN)
