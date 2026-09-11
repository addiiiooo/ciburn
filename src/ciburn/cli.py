"""ciburn command-line interface."""

from __future__ import annotations

import click

from ciburn import __version__

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, "--version", "-V", prog_name="ciburn")
def cli() -> None:
    """ciburn: what your GitHub Actions CI costs, and where the money goes to waste."""


def main() -> None:
    cli(prog_name="ciburn")


if __name__ == "__main__":  # pragma: no cover
    main()
