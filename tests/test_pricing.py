from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ciburn.pricing import (
    Pricing,
    PricingError,
    RunnerClass,
    apply_included_minutes,
    billable_minutes,
    price_job,
    price_minutes,
    rounding_waste_seconds,
)

# --- rounding boundaries: exact, as required by the brief ---------------------


@pytest.mark.parametrize(
    ("seconds", "minutes"),
    [
        (0, 0),
        (1, 1),
        (59, 1),
        (60, 1),
        (61, 2),
        (119, 2),
        (120, 2),
        (121, 3),
        (3600, 60),
        (3601, 61),
    ],
)
def test_rounding_boundaries(seconds: int, minutes: int) -> None:
    assert billable_minutes(seconds) == minutes


def test_negative_and_nan_durations_bill_zero() -> None:
    assert billable_minutes(-5) == 0
    assert billable_minutes(float("nan")) == 0
    assert rounding_waste_seconds(-5) == 0.0
    assert rounding_waste_seconds(float("nan")) == 0.0


@pytest.mark.parametrize(
    ("seconds", "waste"), [(0, 0.0), (1, 59.0), (59, 1.0), (60, 0.0), (61, 59.0)]
)
def test_rounding_waste(seconds: int, waste: float) -> None:
    assert rounding_waste_seconds(seconds) == waste


# --- property tests -------------------------------------------------------------


@given(st.floats(min_value=0, max_value=10**7, allow_nan=False, allow_infinity=False))
def test_billable_minutes_never_negative_and_covers_duration(seconds: float) -> None:
    m = billable_minutes(seconds)
    assert m >= 0
    assert m * 60 >= seconds
    assert (m - 1) * 60 < seconds or m == 0


@given(
    st.floats(min_value=0, max_value=10**6, allow_nan=False, allow_infinity=False),
    st.floats(min_value=0, max_value=10**6, allow_nan=False, allow_infinity=False),
)
def test_billable_minutes_monotonic(a: float, b: float) -> None:
    lo, hi = sorted((a, b))
    assert billable_minutes(lo) <= billable_minutes(hi)


@settings(max_examples=200)
@given(
    st.sampled_from(["2026", "2025", "2026-selfhosted-announced"]),
    st.sampled_from(
        ["actions_linux", "actions_windows", "actions_macos", "linux_8_core", "macos_xl"]
    ),
    st.floats(min_value=0, max_value=10**6, allow_nan=False, allow_infinity=False),
    st.floats(min_value=0, max_value=10**6, allow_nan=False, allow_infinity=False),
)
def test_cost_is_nonnegative_and_monotonic(
    pricing: Pricing, model_id: str, sku: str, a: float, b: float
) -> None:
    model = pricing.model(model_id)
    runner = RunnerClass(kind="hosted", sku=sku, label=sku)
    lo, hi = sorted((a, b))
    p_lo = price_job(model, runner, lo)
    p_hi = price_job(model, runner, hi)
    assert p_lo.cost >= 0
    assert p_lo.cost <= p_hi.cost
    assert p_lo.cost == model.sku(sku).per_minute * p_lo.billed_minutes


# --- pricing correctness against the verified tables ----------------------------


def test_2026_standard_rates(pricing: Pricing) -> None:
    m = pricing.model("2026")
    assert m.sku("actions_linux").per_minute == Decimal("0.006")
    assert m.sku("actions_windows").per_minute == Decimal("0.010")
    assert m.sku("actions_macos").per_minute == Decimal("0.062")
    assert m.sku("actions_linux_slim").per_minute == Decimal("0.002")


def test_2025_standard_rates(pricing: Pricing) -> None:
    m = pricing.model("2025")
    assert m.sku("actions_linux").per_minute == Decimal("0.008")
    assert m.sku("actions_windows").per_minute == Decimal("0.016")
    assert m.sku("actions_macos").per_minute == Decimal("0.08")


def test_platform_charge_is_never_added_on_top(pricing: Pricing) -> None:
    m = pricing.model("2026")
    assert m.platform_charge_included_in_hosted_rates
    runner = RunnerClass(kind="hosted", sku="actions_linux", label="ubuntu-latest")
    assert price_job(m, runner, 600).cost == Decimal("0.060000")


def test_self_hosted_is_free_in_current_model_and_priced_in_scenario(pricing: Pricing) -> None:
    runner = RunnerClass(kind="self_hosted_or_custom", sku=None, label="self-hosted")
    cur = price_job(pricing.model("2026"), runner, 600)
    assert cur.cost == 0
    assert not cur.priced
    assert cur.billed_minutes == 10
    scen = price_job(pricing.model("2026-selfhosted-announced"), runner, 600)
    assert scen.cost == Decimal("0.020000")
    assert scen.priced
    assert not pricing.model("2026-selfhosted-announced").in_effect


def test_public_repo_pricing(pricing: Pricing) -> None:
    m = pricing.model("2026")
    std = RunnerClass(kind="hosted", sku="actions_linux", label="ubuntu-latest")
    big = RunnerClass(kind="hosted", sku="linux_8_core", label="ubuntu-8-core")
    assert price_job(m, std, 600, public_repo=True).cost == 0
    assert price_job(m, big, 600, public_repo=True).cost == Decimal("0.220000")
    sh = RunnerClass(kind="self_hosted_or_custom", sku=None, label="self-hosted")
    assert (
        price_job(pricing.model("2026-selfhosted-announced"), sh, 600, public_repo=True).cost == 0
    )


def test_unknown_hosted_sku_is_unpriced(pricing: Pricing) -> None:
    runner = RunnerClass(kind="hosted", sku=None, label="ubuntu-latest-16-cores")
    jp = price_job(pricing.model("2026"), runner, 600)
    assert not jp.priced
    assert jp.cost == 0
    assert jp.billed_minutes == 10


def test_price_minutes_and_errors(pricing: Pricing) -> None:
    m = pricing.model("2026")
    assert price_minutes(m, "actions_macos", 100) == Decimal("6.200000")
    with pytest.raises(ValueError, match=">= 0"):
        price_minutes(m, "actions_macos", -1)
    with pytest.raises(PricingError, match="unknown SKU"):
        m.sku("nope")
    with pytest.raises(PricingError, match="unknown pricing model"):
        pricing.model("1999")


# --- label classification --------------------------------------------------------


@pytest.mark.parametrize(
    ("labels", "runner_name", "kind", "sku"),
    [
        (["ubuntu-latest"], "GitHub Actions 12", "hosted", "actions_linux"),
        (["ubuntu-24.04"], None, "hosted", "actions_linux"),
        (["ubuntu-22.04-arm"], None, "hosted", "actions_linux_arm"),
        (["ubuntu-slim"], None, "hosted", "actions_linux_slim"),
        (["windows-latest"], None, "hosted", "actions_windows"),
        (["windows-2022"], None, "hosted", "actions_windows"),
        (["windows-11-arm"], None, "hosted", "actions_windows_arm"),
        (["macos-latest"], None, "hosted", "actions_macos"),
        (["macos-15-intel"], None, "hosted", "actions_macos"),
        (["macos-13"], None, "hosted", "actions_macos"),
        (["macos-latest-large"], None, "hosted", "macos_l"),
        (["macos-15-xlarge"], None, "hosted", "macos_xl"),
        (["xcode-27-xlarge"], None, "hosted", "macos_xl"),
        (["self-hosted", "linux"], "my-runner-1", "self_hosted_or_custom", None),
        (["depot-ubuntu-24.04-4"], "depot-vb0d207rjs", "self_hosted_or_custom", None),
        (["ubuntu-latest-16-cores"], "GitHub Actions 3", "hosted", None),
        ([], None, "self_hosted_or_custom", None),
    ],
)
def test_classify(
    pricing: Pricing, labels: list[str], runner_name: str | None, kind: str, sku: str | None
) -> None:
    rc = pricing.classify(labels, runner_name)
    assert rc.kind == kind
    assert rc.sku == sku


def test_classify_override(pricing: Pricing) -> None:
    rc = pricing.classify(
        ["ubuntu-latest-16-cores"], "GitHub Actions 3", {"ubuntu-latest-16-cores": "linux_16_core"}
    )
    assert rc.sku == "linux_16_core"
    assert rc.kind == "hosted"


# --- included minutes -------------------------------------------------------------


def test_included_minutes_offset_is_labelled_an_assumption(pricing: Pricing) -> None:
    m = pricing.model("2026")
    linux = RunnerClass(kind="hosted", sku="actions_linux", label="ubuntu-latest")
    mac = RunnerClass(kind="hosted", sku="actions_macos", label="macos-latest")
    big = RunnerClass(kind="hosted", sku="linux_8_core", label="big")
    jobs = [
        price_job(m, linux, 60 * 1000),
        price_job(m, mac, 60 * 100),
        price_job(m, big, 60 * 100),
    ]
    res = apply_included_minutes(pricing, m, "free", jobs)
    assert res.included_minutes == 2000
    # 1000 linux minutes + 100 mac minutes * (0.062/0.006) = 2033.33 drawdown minutes
    assert res.drawdown_minutes == Decimal("2033.333333")
    assert res.gross_cost == Decimal("6.000000") + Decimal("6.200000") + Decimal("2.200000")
    assert res.covered_cost < Decimal("12.2")  # quota does not cover everything
    assert res.net_cost >= Decimal("2.2")  # larger runner never covered
    assert "assumption" in res.assumption
    with pytest.raises(PricingError, match="unknown plan"):
        apply_included_minutes(pricing, m, "platinum", jobs)


def test_included_minutes_full_coverage(pricing: Pricing) -> None:
    m = pricing.model("2026")
    linux = RunnerClass(kind="hosted", sku="actions_linux", label="ubuntu-latest")
    res = apply_included_minutes(pricing, m, "team", [price_job(m, linux, 60 * 10)])
    assert res.net_cost == 0
    assert res.covered_cost == res.gross_cost
    res0 = apply_included_minutes(pricing, m, "team", [])
    assert res0.gross_cost == 0
    assert res0.covered_cost == 0


def test_scenario_self_hosted_draws_quota(pricing: Pricing) -> None:
    m = pricing.model("2026-selfhosted-announced")
    sh = RunnerClass(kind="self_hosted_or_custom", sku=None, label="self-hosted")
    res = apply_included_minutes(pricing, m, "free", [price_job(m, sh, 60 * 300)])
    assert res.drawdown_minutes == Decimal("100.000000")  # 300 min * 0.002/0.006
    assert res.net_cost == 0


# --- loader validation --------------------------------------------------------------


def test_loader_rejects_bad_data() -> None:
    with pytest.raises(PricingError, match="missing key"):
        Pricing.from_dict({"fetched_at": "2026-01-01T00:00:00Z"})
    raw = dict(Pricing.load().raw)
    models = dict(raw["models"])
    models["x"] = {"name": "x", "source_url": "https://e", "inherits_skus_from": "missing"}
    raw["models"] = models
    with pytest.raises(PricingError, match="unknown model"):
        Pricing.from_dict(raw)
    models["x"] = {"name": "x", "source_url": "https://e", "inherits_skus_from": "y"}
    models["y"] = {"name": "y", "source_url": "https://e", "inherits_skus_from": "x"}
    with pytest.raises(PricingError, match="circular"):
        Pricing.from_dict(raw)


def test_loader_rejects_negative_rate() -> None:
    raw = dict(Pricing.load().raw)
    models = dict(raw["models"])
    m = dict(models["2026"])
    skus = dict(m["skus"])
    skus["actions_linux"] = {"per_minute": -1, "os": "linux"}
    m["skus"] = skus
    models["2026"] = m
    raw["models"] = models
    with pytest.raises(PricingError, match="negative"):
        Pricing.from_dict(raw)


def test_pricing_loads_from_explicit_path(tmp_path: object, fixtures_dir: object) -> None:
    from pathlib import Path

    src = Path(__file__).parents[1] / "src" / "ciburn" / "data" / "pricing.yaml"
    p = Pricing.load(src)
    assert "2026" in p.models
    assert p.rounding_statement.startswith("GitHub rounds")
    assert p.is_stale(p.fetched_at.replace(year=p.fetched_at.year + 1))
    assert not p.is_stale(p.fetched_at)
