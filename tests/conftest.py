from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ciburn.pricing import Pricing

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def pricing() -> Pricing:
    return Pricing.load()


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


def load_api_fixture(name: str) -> Any:
    with (FIXTURES / "api" / name).open(encoding="utf-8") as fh:
        return json.load(fh)
