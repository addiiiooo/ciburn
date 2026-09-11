"""Fails loudly when pricing.yaml is older than 90 days.

This is deliberate: a stale price table must never ship silently. When this
test fails, re-verify every rate in ``src/ciburn/data/pricing.yaml`` against the
source URLs in the file and RESEARCH.md, then update ``fetched_at``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ciburn.pricing import PRICING_MAX_AGE_DAYS, Pricing


def test_pricing_is_fresh(pricing: Pricing) -> None:
    age = pricing.age(datetime.now(UTC))
    assert age.days <= PRICING_MAX_AGE_DAYS, (
        f"pricing.yaml was fetched {age.days} days ago (limit {PRICING_MAX_AGE_DAYS}). "
        "Re-verify every rate against the source URLs in the file and bump fetched_at."
    )


def test_every_block_has_source_and_timestamp(pricing: Pricing) -> None:
    raw = pricing.raw
    for key in ("rounding", "free_usage", "included_minutes", "limits", "label_patterns"):
        assert "source_url" in raw[key], key
        assert "fetched_at" in raw[key], key
    for mid, m in raw["models"].items():
        assert m["source_url"].startswith("https://"), mid
        assert "fetched_at" in m, mid


def test_unverified_skus_are_excluded_from_cost_math() -> None:
    data = Pricing.load().raw
    data = dict(data)
    models = dict(data["models"])
    m2026 = dict(models["2026"])
    skus = dict(m2026["skus"])
    skus["mystery_sku"] = {"per_minute": 9.99, "os": "linux", "unverified": True}
    m2026["skus"] = skus
    models["2026"] = m2026
    data["models"] = models
    p = Pricing.from_dict(data)
    assert "mystery_sku" not in p.model("2026").skus
