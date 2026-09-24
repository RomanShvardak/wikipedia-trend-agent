"""Contract tests: freeze the D-03 metrics.json contract against the golden fixture.

The fixture plus these tests pin the ANAL-06 single source of truth:
the full D-03 field set, `pct: null` never-0 semantics (a null pct always
carries a reason), enums/ranges, and the no-cross-series-rollups rule.

No network and no matplotlib/pandas/requests/numpy anywhere in this file.
Imports `common` through the conftest.py sys.path shim (plan 01 artifact).
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from common import load_spec

METRICS_EXAMPLE = Path(__file__).resolve().parent / "fixtures" / "metrics.example.json"
CONTRACTS_DOC = Path(__file__).resolve().parents[1] / "references" / "CONTRACTS.md"

GROWTH_WINDOWS = ("m3", "y1", "y2")
TREND_DIRECTIONS = {"up", "down", "flat", "noise", "inconclusive"}
CONFIDENCE_LEVELS = {"high", "medium", "low"}
# Cross-series aggregate/rollup keys are forbidden by D-03 (exact key-name match,
# so the per-series field `total_views` is unaffected).
ROLLUP_KEY_BLACKLIST = {"total", "top_series", "aggregate", "totals", "ranking", "overall", "rollup"}


@pytest.fixture(scope="module")
def metrics() -> dict:
    return load_spec(METRICS_EXAMPLE)


def _iter_values(obj):
    """Yield every leaf value in a JSON document."""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_values(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_values(v)
    else:
        yield obj


def _iter_keys(obj):
    """Yield every key in a JSON document."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _iter_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_keys(v)


def _assert_pct_value(window: dict, path: str) -> None:
    """ANAL-06 on one growth window: null pct needs a reason; numeric pct is finite."""
    pct = window.get("pct")
    if pct is None:
        reason = window.get("reason")
        assert isinstance(reason, str) and reason.strip() != "", (
            f"growth.{path}.pct is null but carries no non-empty reason (ANAL-06)"
        )
    else:
        assert isinstance(pct, (int, float)) and math.isfinite(pct), (
            f"growth.{path}.pct must be a finite number, got {pct!r}"
        )


def test_metrics_fixture_loads(metrics) -> None:
    """The golden fixture is a VALID metrics.json: identity keys + non-empty series."""
    assert isinstance(metrics, dict)
    for key in ("spec_name", "as_of", "generated_from"):
        assert key in metrics, f"top-level {key!r} missing"
    assert isinstance(metrics["series"], list) and len(metrics["series"]) > 0


def test_series_match_spec_fixture(metrics, spec_example_path) -> None:
    """Cross-fixture SSoT: metrics series_ids mirror the spec fixture ids, in order."""
    spec = load_spec(spec_example_path)
    spec_ids = [item["id"] for item in spec["series"]]
    metrics_ids = [item["series_id"] for item in metrics["series"]]
    assert metrics_ids == spec_ids, (
        "metrics.json series_ids must equal spec.example.json series ids in the same order"
    )
    assert metrics["spec_name"] == spec["name"], (
        "metrics.json spec_name must equal the spec fixture name"
    )


def test_no_nan_or_inf(metrics) -> None:
    """Every float/int in the fixture is finite; no NaN/Infinity strings anywhere."""
    for value in _iter_values(metrics):
        if isinstance(value, float):
            assert math.isfinite(value), f"non-finite float in metrics.json: {value!r}"
        elif isinstance(value, str):
            assert "NaN" not in value and "Infinity" not in value, (
                f"NaN/Infinity string in metrics.json: {value!r}"
            )


def test_null_pct_has_reason(metrics) -> None:
    """ANAL-06: a null pct always carries a non-empty reason; numeric pcts are finite."""
    for series in metrics["series"]:
        for name in GROWTH_WINDOWS:
            window = series["growth"][name]
            _assert_pct_value(window, f"{series['series_id']}.{name}")
            clean = window.get("clean")
            if isinstance(clean, dict) and "pct" in clean:
                _assert_pct_value(clean, f"{series['series_id']}.{name}.clean")


def test_pct_never_zero_as_missing(metrics) -> None:
    """A literal 0 must never mark 'not computable' (ANAL-06)."""
    for series in metrics["series"]:
        for name in GROWTH_WINDOWS:
            window = series["growth"][name]
            assert window["pct"] != 0, (
                f"growth.{name}.pct is 0 — a literal 0 never marks 'not computable' (ANAL-06)"
            )
            clean = window.get("clean")
            if isinstance(clean, dict) and "pct" in clean:
                assert clean["pct"] != 0, f"growth.{name}.clean.pct is 0 (ANAL-06)"


def test_enum_membership(metrics) -> None:
    """trend_direction/confidence/anomaly_share hold their frozen value sets."""
    for series in metrics["series"]:
        assert series["trend_direction"] in TREND_DIRECTIONS, series["trend_direction"]
        assert series["confidence"] in CONFIDENCE_LEVELS, series["confidence"]
        assert 0.0 <= series["anomaly_share"] <= 1.0, series["anomaly_share"]


def test_clean_variant_present(metrics) -> None:
    """Every window with a numeric pct has a clean {pct, abs} with finite numbers (D-03)."""
    for series in metrics["series"]:
        for name in GROWTH_WINDOWS:
            window = series["growth"][name]
            if window.get("pct") is None:
                continue  # null windows may carry a null clean (D-03)
            clean = window.get("clean")
            assert isinstance(clean, dict), f"growth.{name} with numeric pct lacks clean (D-03)"
            assert "pct" in clean and "abs" in clean, f"growth.{name}.clean shape is {clean!r}"
            assert isinstance(clean["pct"], (int, float)) and math.isfinite(clean["pct"]), (
                f"growth.{name}.clean.pct must be finite, got {clean['pct']!r}"
            )
            assert isinstance(clean["abs"], int) and math.isfinite(float(clean["abs"])), (
                f"growth.{name}.clean.abs must be a finite integer, got {clean['abs']!r}"
            )


def test_no_cross_series_rollups(metrics) -> None:
    """Top-level keys are exactly the D-03 identity set; no rollup keys anywhere."""
    assert set(metrics.keys()) == {"spec_name", "as_of", "generated_from", "series"}, (
        f"top-level keys must be exactly spec_name/as_of/generated_from/series, got {sorted(metrics)}"
    )
    for key in _iter_keys(metrics):
        assert key not in ROLLUP_KEY_BLACKLIST, (
            f"cross-series rollup key found in metrics.json: {key!r}"
        )


def test_contracts_doc_states_pct_rule() -> None:
    """CONTRACTS.md states the 'never 0' rule and the null-requires-reason corollary."""
    text = CONTRACTS_DOC.read_text(encoding="utf-8")
    assert "never 0" in text, "CONTRACTS.md must state the pct rule with the exact phrase 'never 0'"
    assert "must carry" in text, "CONTRACTS.md must name the null-requires-reason corollary"