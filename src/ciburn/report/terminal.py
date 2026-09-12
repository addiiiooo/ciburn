from __future__ import annotations

from rich import box
from rich.console import Console
from rich.padding import Padding
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

# Below this console width the finding tables collapse their text column to a
# dozen characters and wrap every word onto its own line (the macOS Terminal
# default is 80x24), so findings are laid out one per block instead.
TABLE_MIN_WIDTH = 120


def render_terminal(
    result: AuditResult, console: Console | None = None, *, max_rows: int = 40
) -> None:
    console = console or Console()
    wide = console.width >= TABLE_MIN_WIDTH
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
    measured_title = f"MEASURED — from this repository's own run history ({len(measured)})"
    if not measured:
        console.print("MEASURED: no findings with history evidence.", style="bold")
    elif wide:
        t = Table(title=measured_title, box=box.SIMPLE, title_justify="left", expand=True)
        t.add_column("rule")
        t.add_column("sev")
        t.add_column("where", overflow="fold")
        t.add_column("observed", overflow="fold", ratio=1)
        t.add_column("recoverable (estimate)", overflow="fold", ratio=1)
        t.add_column("conf")
        t.add_column("finding", overflow="fold", ratio=3)
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
        console.print(Text(measured_title, style="bold"))
        for f in measured[:max_rows]:
            console.print()
            console.print(_finding_header(f, confidence=True))
            console.print(_indented(_labelled("observed", observed_cell(f))))
            console.print(_indented(_labelled("recoverable", recoverable_cell(f, result))))
            console.print(_indented(_finding_text(f)))

    advisory = result.advisory
    advisory_title = f"ADVISORY — configuration only, no history evidence ({len(advisory)})"
    if not advisory:
        console.print("ADVISORY: none.", style="bold")
    elif wide:
        t = Table(title=advisory_title, box=box.SIMPLE, title_justify="left", expand=True)
        t.add_column("rule")
        t.add_column("sev")
        t.add_column("where", overflow="fold")
        t.add_column("finding", overflow="fold", ratio=3)
        t.add_column("fix", overflow="fold", ratio=2)
        for f in advisory[:max_rows]:
            t.add_row(f.rule_id, _sev(f), _where(f), Text(f.message), Text(f.remediation.summary))
        console.print(t)
    else:
        console.print()
        console.print(Text(advisory_title, style="bold"))
        for f in advisory[:max_rows]:
            console.print()
            console.print(_finding_header(f, confidence=False))
            console.print(_indented(_finding_text(f, method=False)))

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


def _finding_text(f: Finding, *, method: bool = True) -> Text:
    """Message, then (for measured findings) its estimate method, then the fix."""
    parts: list[Text | str] = [f.message]
    if method and f.estimate_method:
        parts += ["\n", Text(f"method: {f.estimate_method}", style="dim")]
    parts += ["\n", Text(f"fix: {f.remediation.summary}", style="green")]
    return Text.assemble(*parts)


def _finding_header(f: Finding, *, confidence: bool) -> Text:
    """One line per finding in the narrow layout: rule, severity, location, confidence."""
    t = Text.assemble((f.rule_id, "bold"), "  ", _sev(f), "  ", (_where(f), "bold"))
    if confidence:
        t.append(f"  confidence {f.confidence.value}", style="dim")
    return t


def _labelled(label: str, value: str) -> Text:
    return Text.assemble((f"{label}: ", "dim"), value)


def _indented(renderable: Text) -> Padding:
    """Indent by two columns; rich wraps the text to the remaining width."""
    return Padding(renderable, (0, 0, 0, 2), expand=False)
