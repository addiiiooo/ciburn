"""Render a real `ciburn audit` terminal report to SVG with rich's Console.save_svg().

No mock-ups: the input is whatever `ciburn audit` produces for the given
repository right now. Usage:

    GITHUB_TOKEN=... python scripts/render_demo.py --repo pallets/flask --days 30 --out docs/demo.svg
"""

from __future__ import annotations

import argparse
from pathlib import Path

from rich.console import Console

from ciburn.audit import AuditOptions, run_audit
from ciburn.report.terminal import render_terminal


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--out", type=Path, default=Path("docs/demo.svg"))
    ap.add_argument("--width", type=int, default=132)
    ap.add_argument("--max-rows", type=int, default=8)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--cache", type=Path, default=None)
    args = ap.parse_args()
    result = run_audit(AuditOptions(repo=args.repo, days=args.days, offline=args.offline, cache=args.cache))
    console = Console(record=True, width=args.width, force_terminal=True, color_system="truecolor")
    render_terminal(result, console, max_rows=args.max_rows)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    console.save_svg(str(args.out), title=f"ciburn audit --repo {args.repo} --days {args.days}")
    print(f"wrote {args.out} ({args.out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
