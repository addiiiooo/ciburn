from __future__ import annotations

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from ciburn.audit import AuditResult
from ciburn.findings import Finding, Severity
from ciburn.report.common import (
    headline,
    money,
    observed_cell,
    recoverable_cell,
    top_groups,
    totals_lines,
)

SEV_STYLE = {Severity.HIGH: "bold red", Severity.MEDIUM: "yellow", Severity.LOW: "cyan"}


def render_terminal(
    result: AuditResult, console: Console | None = None, *, max_rows: int = 40
) -> None:
    console = console or Console()
    console.print(Text(headline(result), style="bold"))
    if result.error:
        console.print(Text(f"error: {result.error}", style="bold red"))
    for line in totals_lines(result):
        console.print(line, highlight=False)
    console.print()

    groups = top_groups(result)
    if groups:
        t = Table(
            title="Where the minutes go (top job groups)", box=box.SIMPLE, title_justify="left"
        )
        t.add_column("workflow", overflow="fold")
        t.add_column("job", overflow="fold")
        t.add_column("jobs", justify="right")
        t.add_column("billed min", justify="right")
        t.add_column("list price", justify="right")
        t.add_column("p50 min", justify="right")
        for path, base, n, mins, cost, p50 in groups:
            t.add_row(
                path.removeprefix(".github/workflows/"),
                base,
                str(n),
                f"{mins:,}",
                money(cost),
                f"{p50:.1f}",
            )
        console.print(t)

    measured = result.measured
    if measured:
        t = Table(
            title=f"MEASURED — from this repository's own run history ({len(measured)})",
            box=box.SIMPLE,
            title_justify="left",
        )
        t.add_column("rule")
        t.add_column("sev")
        t.add_column("where", overflow="fold")
        t.add_column("observed", overflow="fold")
        t.add_column("recoverable (estimate)", overflow="fold")
        t.add_column("conf")
        t.add_column("finding", overflow="fold", ratio=2)
        for f in measured[:max_rows]:
            t.add_row(
                f.rule_id,
                _sev(f),
                _where(f),
                observed_cell(f),
                recoverable_cell(f, result),
                f.confidence.value,
                _finding_text(f),
            )
        console.print(t)
    else:
        console.print("MEASURED: no findings with history evidence.", style="bold")

    advisory = result.advisory
    if advisory:
        t = Table(
            title=f"ADVISORY — configuration only, no history evidence ({len(advisory)})",
            box=box.SIMPLE,
            title_justify="left",
        )
        t.add_column("rule")
        t.add_column("sev")
        t.add_column("where", overflow="fold")
        t.add_column("finding", overflow="fold", ratio=2)
        t.add_column("fix", overflow="fold")
        for f in advisory[:max_rows]:
            t.add_row(f.rule_id, _sev(f), _where(f), f.message, f.remediation.summary)
        console.print(t)
    else:
        console.print("ADVISORY: none.", style="bold")

    if result.notes:
        console.print()
        for note in result.notes:
            console.print(f"note: {note}", style="dim", highlight=False)
    console.print(
        "\nEstimates are labelled as such; each carries its method (see --format json or md). "
        f"Rates: {result.model.source_url} (fetched {result.pricing.fetched_at:%Y-%m-%d}).",
        style="dim",
        highlight=False,
    )


def _sev(f: Finding) -> Text:
    return Text(f.severity.value, style=SEV_STYLE[f.severity])


def _where(f: Finding) -> str:
    wf = (f.workflow or "").removeprefix(".github/workflows/")
    return f"{wf}#{f.job}" if f.job else wf or "(repo)"


def _finding_text(f: Finding) -> str:
    s = f.message
    if f.estimate_method:
        s += f"\n[dim]method: {f.estimate_method}[/dim]"
    s += f"\n[green]fix: {f.remediation.summary}[/green]"
    return s
