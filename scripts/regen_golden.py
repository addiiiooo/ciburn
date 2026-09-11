"""Regenerate tests/fixtures/expected/*.json from the current static rules.

Run, then REVIEW THE DIFF BY HAND. A golden file is a reviewed statement of what
the rules should say about a fixture, not a snapshot of whatever they said last.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from ciburn.pricing import Pricing
from test_static_rules import (  # type: ignore[import-not-found]
    EXPECTED_DIR,
    FIXTURES,
    analyse,
    summary,
)


def main() -> None:
    pricing = Pricing.load()
    EXPECTED_DIR.mkdir(parents=True, exist_ok=True)
    for name in FIXTURES:
        out = EXPECTED_DIR / f"{name}.json"
        out.write_text(
            json.dumps(summary(analyse(name, pricing)), indent=2) + "\n", encoding="utf-8"
        )
        print(f"{name}: {[d['rule'] for d in summary(analyse(name, pricing))]}")


if __name__ == "__main__":
    main()
