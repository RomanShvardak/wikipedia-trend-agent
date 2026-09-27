"""Contract tests: freeze the D-03 metrics.json contract against the golden fixture.

The fixture plus these tests pin the ANAL-06 single source of truth:
the full D-03 field set, `pct: null` never-0 semantics (a null pct always
carries a reason), enums/ranges, and the no-cross-series-rollups rule.

Plan 05-07 added the `charts.v1` section: a document-completeness test, a
field-drift test bound to the real renderer, and a cross-fixture key-set test
for the spike-injected metrics fixture. The drift test genuinely renders, so
this file now imports `make_charts` — whose matplotlib import lives INSIDE its
render function, so the module-level import below pulls no rendering backend,
and the assertions are on the manifest document and the fixture shape only,
never on a PNG's bytes or pixels.

No network anywhere, and no direct pandas/requests/numpy import.
Imports `common` and `make_charts` through the conftest.py sys.path shim
(plan 01 artifact).
"""
from __future__ import annotations

import ast
import json
import math
from datetime import date
from pathlib import Path
from typing import Any

import pytest

import build_report
import make_charts
import resolve_articles
from common import load_and_validate_spec, load_spec

METRICS_EXAMPLE = Path(__file__).resolve().parent / "fixtures" / "metrics.example.json"
METRICS_ANOMALIES_EXAMPLE = (
    Path(__file__).resolve().parent / "fixtures" / "metrics.anomalies.example.json"
)
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
CONTRACTS_DOC = Path(__file__).resolve().parents[1] / "references" / "CONTRACTS.md"
SKILL_DOC = Path(__file__).resolve().parents[1] / "SKILL.md"

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


def test_pct_null_or_zero_semantics() -> None:
    """Numeric zero means unchanged; null requires a non-empty own reason."""
    _assert_pct_value({"pct": 0.0, "abs": 0, "clean": {"pct": 0.0, "abs": 0}}, "zero")
    with pytest.raises(AssertionError):
        _assert_pct_value({"pct": None, "abs": None}, "missing_outer_reason")
    with pytest.raises(AssertionError):
        _assert_pct_value({"pct": None, "abs": None, "reason": "  "}, "blank_outer_reason")
    with pytest.raises(AssertionError):
        _assert_pct_value({"pct": None, "abs": None}, "missing_clean_reason")


def test_numeric_y2_requires_executable_history(metrics) -> None:
    """A numeric two-year comparison requires the inclusive 1460-day span."""
    for series in metrics["series"]:
        y2 = series["growth"]["y2"]
        if y2["pct"] is not None:
            assert series["period"]["days"] >= 1460
            assert isinstance(y2["clean"], dict)
            assert y2["clean"]["pct"] is not None


def test_728_day_golden_y2_is_not_computable(metrics) -> None:
    """The committed 728-day example cannot claim numeric two-year growth."""
    for series in metrics["series"]:
        if series["period"]["days"] < 1460:
            y2 = series["growth"]["y2"]
            assert y2["pct"] is None
            assert y2["abs"] is None
            assert y2["reason"] == "insufficient observations in one or both equal-length windows"
            assert y2["clean"]["pct"] is None
            assert y2["clean"]["abs"] is None
            assert y2["clean"]["reason"] == y2["reason"]


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


def test_resolver_help_publishes_every_flag_and_verbose_semantics() -> None:
    help_text = resolve_articles._parser().format_help()

    for flag in (
        "--topic",
        "--projects",
        "--topic-for",
        "--out",
        "--select",
        "--reason",
        "--ttl-hours",
        "--verbose",
    ):
        assert flag in help_text, f"resolver help must publish {flag}"
    assert "logging-only" in help_text
    assert "adds no fields" in help_text
    assert "WTI_TTL_HOURS or 24.0" in help_text
    assert "0 forces refetch" in help_text


def test_resolver_ttl_executable_default_environment_and_precedence(
    tmp_path, monkeypatch
) -> None:
    base = ["--topic", "topic", "--projects", "en.wikipedia", "--out", str(tmp_path / "r.json")]
    monkeypatch.delenv("WTI_TTL_HOURS", raising=False)
    assert resolve_articles._parser().parse_args(base).ttl_hours == 24.0
    monkeypatch.setenv("WTI_TTL_HOURS", "8.5")
    assert resolve_articles._parser().parse_args(base).ttl_hours == 8.5
    assert (
        resolve_articles._parser().parse_args([*base, "--ttl-hours", "0"]).ttl_hours
        == 0.0
    )


def test_resolved_v1_contract_section_is_complete() -> None:
    text = CONTRACTS_DOC.read_text(encoding="utf-8")

    for marker in (
        "resolved.v1",
        "search_no_hits",
        "no_selectable_candidates",
        "ordered `search_hits`",
        "MAX_PROJECTS=8",
        "MAX_RESPONSE_BYTES=1_048_576",
        "read(MAX_RESPONSE_BYTES + 1)",
        "all-access",
        "WTI_TTL_HOURS",
        "logging-only",
        "last completed atomic replace wins",
        "caller-serialized",
    ):
        assert marker in text, f"resolved.v1 contract must document {marker!r}"


def test_skill_publishes_two_run_resolver_workflow() -> None:
    text = SKILL_DOC.read_text(encoding="utf-8")

    for marker in (
        "resolve_articles.py",
        "--topic-for",
        "--select",
        "--ttl-hours",
        "--verbose",
        "logging-only",
        'status="confirmed"',
        "references/CONTRACTS.md",
    ):
        assert marker in text, f"SKILL.md must document {marker!r}"
    assert text.index("--topic-for") < text.index('status="confirmed"')


def test_frozen_spec_and_metrics_fixture_key_sets_remain_unchanged(
    metrics, spec_example_path
) -> None:
    spec = load_spec(spec_example_path)

    assert set(spec) == {
        "name",
        "request",
        "language",
        "window",
        "series",
        "assumptions",
        "quality",
    }
    assert all(
        set(series) == {"id", "project", "article", "label", "language"}
        for series in spec["series"]
    )
    assert set(metrics) == {"spec_name", "as_of", "generated_from", "series"}
    assert metrics["spec_name"] == spec["name"]


# --- Plan 05-07 Task 3: charts.v1 is a frozen contract, so freeze it here ---


def _render_two_series_manifest(tmp_out: Path) -> dict[str, Any]:
    """Render the committed two-series fixture into tmp_out and return charts.json.

    The fixtures are inputs, never scratch files, so the committed
    `series.example.csv` and `metrics.example.json` are copied verbatim: the
    two-series golden already matches `spec.example.json` exactly, so no
    filtering is needed and the manifest this test reads is the one a real
    `make_charts.py` run against the golden inputs produces.
    """
    tmp_out.joinpath("series.csv").write_bytes(
        FIXTURES_DIR.joinpath("series.example.csv").read_bytes()
    )
    tmp_out.joinpath("metrics.json").write_bytes(
        FIXTURES_DIR.joinpath("metrics.example.json").read_bytes()
    )
    spec_path = FIXTURES_DIR / "spec.example.json"
    assert make_charts.main(["--spec", str(spec_path), "--out", str(tmp_out)]) == 0
    return json.loads(tmp_out.joinpath("charts.json").read_text(encoding="utf-8"))


# The frozen charts.v1 top-level surface: exactly these seven, no more.
CHARTS_V1_TOP_LEVEL = {
    "contract_version",
    "spec_name",
    "as_of",
    "language",
    "generated_from",
    "metrics_sha256",
    "charts",
}

# The frozen per-chart union, as a CEILING. No single entry carries all 18; the
# names partition 11 always-emitted / 6 kind-or-mode-conditional / 1
# value-conditional, and this set is the union of those partitions.
CHARTS_V1_UNION = {
    # always emitted (11)
    "kind",
    "label",
    "language",
    "filename",
    "yscale",
    "y_limits",
    "points",
    "gaps",
    "anomalies_drawn",
    "subtitle",
    "log_masked_points",
    # owned by a kind or a mode (6)
    "series_id",
    "spec_index",
    "bars",
    "series_ids",
    "note",
    "log_note",
    # owned by a value (1) - present iff the entry has a date axis
    "x_limits",
}

# 05-02 froze the no-null rule, and the three partitions fall straight out of
# it: a key whose value is null or meaningless for that kind is OMITTED, so a
# test that demanded all 18 on every entry would fail a correct emitter on its
# first default run. Each partition therefore asserts only what the phase's own
# emission rules establish, and names the rule in its failure message.
CHARTS_V1_ALWAYS = {
    "kind",
    "label",
    "language",
    "filename",
    "yscale",
    "y_limits",
    "points",
    "gaps",
    "anomalies_drawn",
    "subtitle",
    "log_masked_points",
}
CHARTS_V1_PER_KIND = {
    # 05-02 forbids emitting a null, and 05-03 gives the overlay
    # series_id = None and no single spec.series[] position, so the overlay
    # publishes neither key.
    "timeseries": {"series_id", "spec_index"},
    # 05-02 leaves bars = () on the non-growth kinds and 05-04 populates it
    # only for growth, so it is absent elsewhere rather than null.
    "growth": {"series_id", "spec_index", "bars"},
    # series_ids/note are overlay-only for the same null-omission reason;
    # the emitter guards note with `note is not None`, not a kind test.
    "overlay": {"series_ids", "note"},
}


def test_charts_v1_contract_section_is_complete() -> None:
    """CONTRACTS.md section 7 exists and states the whole frozen surface.

    A deletion of section 7, or a rewrite that drops any one of these markers,
    fails here. The `## Versioning` block this plan is discharging is
    deliberately NOT re-asserted here: it is the rule, not the contract.
    """
    text = CONTRACTS_DOC.read_text(encoding="utf-8")

    for marker in (
        # the section and its version
        "## 7. `charts.json` — `charts.v1`",
        "charts.v1",
        "contract_version",
        "metrics_sha256",
        "generated_from",
        # the three kinds and the inventory
        "timeseries",
        "growth",
        "overlay",
        "2N+1",
        # the deterministic filename rule
        "chart_<series_id>_<kind>.png",
        "chart_overlay.png",
        "series_lines",
        # the never-recompute rule
        "avg_daily_views",
        "clean.pct",
        "anomalies[]",
        "7-day rolling median",
        "never recomputes",
        # axis / null / log disclosure
        "nonpositive=\"mask\"",
        "log_masked_points",
        "log_note",
        "bar_null",
        "x_limits",
        # the 18-key partition, stated as a partition
        "Always emitted (11)",
        "Owned by a kind or a mode (6)",
        "Owned by a value (1)",
        # integrity wording, atomicity, exit table, CLI, fixtures
        "integrity and staleness mechanism",
        "caller-serialized",
        "last completed atomic replace wins",
        "--log-scale",
        "No test may reach the network",
    ):
        assert marker in text, f"charts.v1 contract must document {marker!r}"


def test_charts_json_emits_only_documented_fields(tmp_out: Path, metrics) -> None:
    """A rendered manifest matches CONTRACTS.md section 7's documented field list.

    Binds the document to the real renderer, and fails in BOTH drift
    directions: an emitted field section 7 does not document, and a
    documented field the emitter stops writing. Asserted on the manifest
    document only - never on PNG bytes or pixels, which are not a stable
    surface across matplotlib/freetype versions.
    """
    manifest = _render_two_series_manifest(tmp_out)

    # Exactly the seven documented top-level keys: no eighth field.
    assert set(manifest) == CHARTS_V1_TOP_LEVEL, (
        f"charts.json top-level keys must be exactly {sorted(CHARTS_V1_TOP_LEVEL)}, "
        f"got {sorted(manifest)} - an undocumented field is drift the next phase "
        "would build on"
    )
    assert manifest["contract_version"] == "charts.v1"

    charts = manifest["charts"]
    for entry in charts:
        kind = entry["kind"]
        # Drift OUT of the document: the union is the ceiling of what any
        # conforming entry may ever carry, so this half is unconditional.
        assert set(entry) <= CHARTS_V1_UNION, (
            f"{kind} {entry.get('filename')}: keys outside the documented 18-name "
            f"union: {sorted(set(entry) - CHARTS_V1_UNION)}"
        )
        assert {"kind", "filename"} <= set(entry), (
            f"{entry.get('filename')}: kind and filename identify a chart and are "
            "never optional"
        )
        assert "series_lines" not in entry, (
            "the overlay's per-series render data is render-only (05-03) and must "
            "never reach charts.json"
        )

        # Drift OUT of the emitter: presence, but only where the phase's own
        # emission rules require it. Each omission is named so a future reader
        # can see why the key is not demanded unconditionally.
        required = CHARTS_V1_ALWAYS | CHARTS_V1_PER_KIND[kind]
        assert required <= set(entry), (
            f"{kind} {entry.get('filename')}: documented keys this kind must carry "
            f"are missing: {sorted(required - set(entry))}"
        )
        if entry["yscale"] == "log":
            # 05-06: log_note is present exactly when the axis is log.
            assert "log_note" in entry, (
                f"{entry.get('filename')}: yscale is log, so log_note must be present"
            )
        else:
            # 05-06 makes log_note a `str | None` that the manifest omits when
            # null, so a default (linear) run carries it nowhere. Asserting its
            # presence unconditionally would reject a correct emitter.
            assert "log_note" not in entry, (
                f"{entry.get('filename')}: yscale is {entry['yscale']!r}, so log_note "
                "must be omitted rather than nulled (the charts.v1 no-null rule)"
            )
        # 05-07: x_limits is guarded by `x_limits is not None`, not by kind.
        # The dated kinds carry the D-10 domain; the growth chart has no date
        # axis and therefore no domain to publish.
        if kind == "growth":
            assert "x_limits" not in entry, (
                "the growth chart's x axis is a percentage axis, so it has no "
                "date domain and x_limits must be omitted, not nulled"
            )
        else:
            domain = entry["x_limits"]
            assert isinstance(domain, list) and len(domain) == 2, (
                f"{entry.get('filename')}: x_limits must publish exactly two bounds, "
                f"got {domain!r}"
            )
            assert all(isinstance(bound, str) for bound in domain), (
                f"{entry.get('filename')}: x_limits bounds must be ISO date strings, "
                f"got {domain!r} - a consumer must parse them without a matplotlib import"
            )
            start, end = (date.fromisoformat(bound) for bound in domain)
            assert start <= end, (
                f"{entry.get('filename')}: x_limits runs backwards, {domain!r}"
            )

        # The bar object is frozen to the same seven names.
        for bar in entry.get("bars", []):
            assert set(bar) <= {
                "window",
                "label",
                "pct",
                "abs",
                "base_avg_daily_views",
                "reason",
                "bar_null",
            }, f"undocumented growth-bar key: {sorted(bar)}"
            assert {"window", "label", "base_avg_daily_views", "bar_null"} <= set(bar), (
                f"a bar must always carry its window, label, base and null marker, "
                f"got {sorted(bar)}"
            )
            if bar["bar_null"]:
                # A null growth is marked, never zero, and always says why.
                assert "pct" not in bar and "reason" in bar, (
                    "a bar_null bar carries no pct and must carry the contract reason"
                )
            else:
                assert "pct" in bar, "a non-null bar must carry its clean percentage"

    # The 2N+1 inventory, against the golden fixture's own series count.
    series_count = len(metrics["series"])
    assert len(charts) == 2 * series_count + 1, (
        f"section 7's 2N+1 rule requires {2 * series_count + 1} charts for "
        f"{series_count} series, got {len(charts)}"
    )
    assert charts[-1]["kind"] == make_charts.OVERLAY_KIND, (
        "the overlay is published last, so a consumer can render it as the "
        "headline without sorting"
    )
    # Every published filename names a PNG that exists, one for one.
    published = {entry["filename"] for entry in charts}
    on_disk = {png.name for png in tmp_out.glob("*.png")}
    assert published == on_disk, (
        f"charts.json names PNGs that do not match the output directory: "
        f"{sorted(published ^ on_disk)}"
    )
    for entry in charts:
        assert "/" not in entry["filename"] and ".." not in entry["filename"], (
            f"filename must be a bare relative name, got {entry['filename']!r}"
        )


def test_anomalies_metrics_fixture_matches_the_frozen_key_set(metrics) -> None:
    """The spike-injected metrics fixture is the golden one, structurally.

    The two differ only in the anomaly-dependent values. Every key set is
    compared KEY-FOR-KEY against the golden rather than against a count
    literal, so this tracks the frozen `metrics.v1` contract instead of a
    number this plan would have to keep re-deriving.
    """
    anomalies = load_spec(METRICS_ANOMALIES_EXAMPLE)

    # Same top-level shape as the golden - no field added or dropped.
    assert set(anomalies) == set(metrics), (
        f"the anomalies fixture's top-level keys must equal the golden's, got "
        f"{sorted(set(anomalies) ^ set(metrics))}"
    )

    # Same series ids in the same order: CONTRACTS.md section 3 - the ids mirror
    # spec.series[] and consumers must never reorder them.
    assert [node["series_id"] for node in anomalies["series"]] == [
        node["series_id"] for node in metrics["series"]
    ], "the anomalies fixture must keep the golden's series ids in the golden's order"

    for candidate, golden in zip(anomalies["series"], metrics["series"]):
        assert set(candidate) == set(golden), (
            f"{golden['series_id']}: the anomalies fixture's per-series key set must "
            f"equal the golden's, differing on {sorted(set(candidate) ^ set(golden))}"
        )

    # The same null-never-0 discipline the golden is held to, reusing the same
    # helper rather than writing a second mechanism.
    for series in anomalies["series"]:
        for name in GROWTH_WINDOWS:
            window = series["growth"][name]
            _assert_pct_value(window, f"{series['series_id']}.{name}")
            clean = window.get("clean")
            if isinstance(clean, dict) and "pct" in clean:
                _assert_pct_value(clean, f"{series['series_id']}.{name}.clean")

    # No NaN/Infinity float and no "None"/"nan" string - the same walk
    # test_no_nan_or_inf performs over the golden.
    for value in _iter_values(anomalies):
        if isinstance(value, float):
            assert math.isfinite(value), f"non-finite float in the anomalies fixture: {value!r}"
        elif isinstance(value, str):
            assert value not in {"None", "nan"} and "Infinity" not in value, (
                f"the anomalies fixture leaked {value!r}"
            )

    # What makes this fixture worth having: the cs series is untouched and so is
    # byte-identical to the golden, while the pl series carries a real anomaly
    # the golden's empty list could never make testable.
    by_id = {node["series_id"]: node for node in anomalies["series"]}
    golden_by_id = {node["series_id"]: node for node in metrics["series"]}
    assert by_id["cs-pust-prerusovany"] == golden_by_id["cs-pust-prerusovany"], (
        "the cs series must be byte-identical to the golden's - the two fixtures "
        "differ only in the anomaly-dependent values"
    )
    assert by_id["pl-post-przerywany"]["anomalies"], (
        "the pl series must carry a non-empty anomalies[] or the mandatory overlay "
        "branch is untestable"
    )
    assert golden_by_id["pl-post-przerywany"]["anomalies"] == [], (
        "the golden fixture is expected to have no anomalies; if it now does, the "
        "pair no longer isolates the anomaly-dependent values"
    )
    # The raw/clean divergence that a recomputation would erase.
    pl = by_id["pl-post-przerywany"]["growth"]["y1"]
    assert pl["pct"] != pl["clean"]["pct"], (
        "the y1 window's raw and clean percentages must differ, which is the whole "
        "reason the never-recompute rule is testable on this fixture"
    )


# The frozen report.v1 top-level surface: exactly these ten, no more.
# Ratified by plan 06-01's Task 1 checkpoint:decision (D-18's successor), and
# bound to the real emitter by the field-drift test 06-03 adds beside the
# charts.v1 one - an undocumented field is drift Phase 7 would build on.
REPORT_V1_TOP_LEVEL = frozenset(
    {
        "contract_version",
        "spec_name",
        "as_of",
        "language",
        "generated_from",
        "metrics_sha256",
        "charts_sha256",
        "report_filename",
        "metrics_shown",
        "formats",
    }
)


# --- 09-02 / 09-05: the formats[] rule is DEFINED IN PRODUCTION --------------------
#
# It began here, in wave 1, because the two tests that needed it were the two that went red
# when the PDF stage arrived. Wave 4 moved it into `build_report`, and the direction of that
# move is the point: PRODUCTION owns the contract and its own writer enforces it, and the
# tests verify the rule rather than supplying it. A rule that lives only in a test can be
# satisfied while the code that writes the document breaks it - and the writer is the thing
# that has to obey.
from build_report import (  # noqa: E402
    REPORT_V1_FORMATS,
    RATIFIED_PDF_FORMAT,
    RATIFIED_V1_FORMATS,
    assert_formats_contract,
)

def test_report_v1_contract_section_is_complete() -> None:
    """CONTRACTS.md section 8 exists and states the whole frozen report.v1 surface.

    A deletion of section 8, or a rewrite that drops any one of these markers,
    fails here. The `## Versioning` block this plan is discharging is
    deliberately NOT re-asserted: it is the rule, not the contract.
    """
    text = CONTRACTS_DOC.read_text(encoding="utf-8")

    for marker in (
        # the section and its version
        "## 8.",
        "report.v1",
        "report.md",
        "report.manifest.json",
        "contract_version",
        "report_filename",
        "metrics_shown",
        "formats",
        "charts_sha256",
        "metrics_sha256",
        "generated_from",
        # the digests are integrity, never authentication
        "integrity and staleness mechanism",
        "without comparing timestamps",
        "not a security control",
        # the never-recompute rule
        "clean.pct",
        "avg_daily_views",
        "not-computable",
        # the six sections, in the frozen order
        "Висновок",
        "Метрики",
        "Наскільки можна довіряти",
        "Графіки",
        "Обмеження та припущення",
        "Наступний крок",
        # the 2N+1 image inventory
        "2N+1",
        # atomicity, exit table, CLI, fixtures
        "caller-serialized",
        "last completed atomic replace wins",
        "python scripts/build_report.py",
        "--verbose",
        "No test may reach the network",
        # 8.1.1, the formats[] membership rule ratified by Phase 9. The markers are
        # chosen to be phrases that appear ONLY in 8.1.1: a marker already satisfied
        # elsewhere in the document is not a gate (Phase 8's "a gate satisfied by an
        # unrelated sentence is not a gate").
        "8.1.1 The `formats[]` membership rule",
        "closed set of shapes",
        "No other `format` value is legal at this version",
        "Appending an array element is NOT a contract version bump",
        '{"format": "pdf", "filename": "report.pdf"}',
    ):
        assert marker in text, f"report.v1 contract must document {marker!r}"


# --- 06-03 Task 2: bind CONTRACTS.md 8.1 to the emitter in BOTH directions ---


def _publish_report_manifest(tmp_out: Path) -> dict[str, Any]:
    """Run the REAL chart stage then the REAL report stage, and read the manifest.

    Both halves are the production emitters, in `tmp_out`, over the committed
    golden inputs. A hand-written `report.manifest.json` would let this test pass
    against a document no code writes, which is the whole failure a field-drift
    test exists to catch - it would be asserting the constant against itself.
    """
    tmp_out.mkdir(parents=True, exist_ok=True)
    assert _render_two_series_manifest(tmp_out), "the chart stage must run first"
    spec_path = FIXTURES_DIR / "spec.example.json"
    assert build_report.main(["--spec", str(spec_path), "--out", str(tmp_out)]) == 0, (
        "the report stage must publish a manifest for this test to read"
    )
    return json.loads(
        tmp_out.joinpath(build_report.REPORT_MANIFEST_FILENAME).read_text(encoding="utf-8")
    )


def test_report_manifest_emits_only_documented_fields(tmp_out: Path) -> None:
    """A published manifest matches CONTRACTS.md 8.1's documented field list.

    The charts.v1 test above is the model, and it binds a document to an
    emitter in BOTH drift directions: an emitted field 8.1 does not document,
    and a documented field the emitter stops writing. Exact set equality is
    symmetric, so one assertion catches both — a dropped key fails as surely as
    an added one.

    Exact set equality is a SHAPE check, though, and a shape check passes on a
    manifest full of plausible wrong values. The value assertions are what make
    this a contract: both digests are re-computed here from the bytes on disk,
    the two identity fields are read back out of `metrics.json`, and the language
    is read out of the validated spec — so an emitter that carried a digest of
    the wrong file, or an `as_of` from a different document, fails here even
    though every key is present and correctly named.
    """
    import hashlib

    manifest = _publish_report_manifest(tmp_out)
    metrics_document = json.loads(tmp_out.joinpath("metrics.json").read_text(encoding="utf-8"))
    spec = load_and_validate_spec(FIXTURES_DIR / "spec.example.json")

    # --- drift OUT of the document, and drift OUT of the emitter, in one check
    assert set(manifest) == REPORT_V1_TOP_LEVEL, (
        f"report.manifest.json top-level keys must be exactly "
        f"{sorted(REPORT_V1_TOP_LEVEL)}, got {sorted(manifest)} - an undocumented "
        f"field is drift Phase 7's run_all.py would build on, and a documented "
        f"field the emitter stopped writing is drift a consumer would hit first"
    )
    assert manifest["contract_version"] == "report.v1", (
        f"the version literal is {manifest['contract_version']!r}, section 8.1 "
        "froze 'report.v1' and anything else is rejected"
    )

    # --- the two digests, re-derived from the files this run published --------
    assert manifest["metrics_sha256"] == hashlib.sha256(
        tmp_out.joinpath("metrics.json").read_bytes()
    ).hexdigest(), (
        "metrics_sha256 must be the sha256 of the metrics.json bytes on disk; a "
        "digest of any other content would make the staleness decision a lie"
    )
    assert manifest["charts_sha256"] == hashlib.sha256(
        tmp_out.joinpath("charts.json").read_bytes()
    ).hexdigest(), (
        "charts_sha256 must be the sha256 of the charts.json bytes on disk, for "
        "the same reason - and 06-03's staleness check reads this field"
    )

    # --- the three identity fields, read back out of their own sources --------
    assert manifest["spec_name"] == metrics_document["spec_name"], (
        f"spec_name must be copied verbatim from metrics.json's own "
        f"{metrics_document['spec_name']!r}, got {manifest['spec_name']!r}"
    )
    assert manifest["as_of"] == metrics_document["as_of"], (
        f"as_of must be copied verbatim from metrics.json's own "
        f"{metrics_document['as_of']!r}, got {manifest['as_of']!r}"
    )
    assert manifest["language"] == spec["language"], (
        f"language is the DOCUMENT language and comes from the validated spec's "
        f"{spec['language']!r}, got {manifest['language']!r}"
    )

    # --- the two shapes a consumer resolves against the directory it was given
    assert manifest["report_filename"] == "report.md", (
        f"report_filename must be the bare relative name 'report.md', got "
        f"{manifest['report_filename']!r}"
    )
    # 09-02: the whole formats[] rule, all five clauses, in one call. This USED to be
    # `== [{"format": "markdown", ...}]`, which the PDF stage's whole purpose turned
    # red. The property worth freezing was never "exactly one element" -- it is "a
    # closed set of shapes", and 8.1.1 states which. `assert_formats_contract` is
    # imported by test_report.py too, so there is one rule and not two.
    assert_formats_contract(manifest["formats"])
    # --- the v1 writer's OWN output is unchanged by the ratification -----------
    assert manifest["formats"] == RATIFIED_V1_FORMATS, (
        f"Phase 9 appended a second legal shape; it did not change what the report "
        f"stage writes, which is still {RATIFIED_V1_FORMATS!r}, got "
        f"{manifest['formats']!r}"
    )
    # --- clause 5, asserted here where the manifest is in hand ---------------
    assert manifest["contract_version"] == "report.v1", (
        "8.1.1 clause 5: appending a formats[] element is not a contract version "
        f"bump, so the literal stays 'report.v1', got "
        f"{manifest['contract_version']!r}"
    )

    shown = manifest["metrics_shown"]
    assert isinstance(shown, list) and shown, (
        f"metrics_shown is the pointer list that turns 'every required number is "
        f"present' into a set comparison; an empty one makes that impossible: {shown!r}"
    )
    assert all(isinstance(pointer, str) for pointer in shown), (
        f"every metrics_shown entry is a string path, got {shown!r}"
    )
    assert len(shown) == len(set(shown)), (
        f"metrics_shown carries duplicates, so a consumer's set comparison depends "
        f"on how many sections quoted the same value: {shown!r}"
    )


# --- 09-02 Task 3: the ratified APPEND is accepted, before any PDF code exists ----
#
# This test runs in 09-02, BEFORE plan 09-05 writes the stage that appends. That order
# is the whole point: a rule amended in the same commit as the code that first needs it
# is a rule that was never proven to accept the new shape. Here the contract accepts the
# append on its own evidence, and 09-05's code must then obey a standing property rather
# than dictate a new one to a test rewritten to accept it.


def test_the_formats_append_of_a_pdf_rendering_is_legal() -> None:
    """The second legal array -- Markdown plus an appended PDF entry -- is accepted.

    Positive half. The three probes below are the negative half, and per STATE.md a
    positive assertion is only meaningful beside a rule shown to refuse.

    The manifest here is a literal, not a produced one, on purpose: the producing code
    is plan 09-05 and asserting against a hand-written document would prove nothing
    about the emitter. What it proves is the CONTRACT: that the array 09-05 will write
    is legal today, under a rule that is already frozen, and that the top-level surface
    and the version literal are untouched by it (clauses 5, and REPORT_V1_TOP_LEVEL).
    """
    appended = [
        {"format": "markdown", "filename": "report.md"},
        {"format": "pdf", "filename": "report.pdf"},
    ]
    assert_formats_contract(appended)

    # The append is an ARRAY change and nothing else. Clause 5: not a version bump.
    manifest = {
        "contract_version": "report.v1",
        **{key: None for key in REPORT_V1_TOP_LEVEL if key != "contract_version"},
        "formats": appended,
    }
    assert set(manifest) == REPORT_V1_TOP_LEVEL, (
        f"the append introduced a top-level key: {sorted(set(manifest) - REPORT_V1_TOP_LEVEL)}"
    )
    assert manifest["contract_version"] == "report.v1"

    # And the entry is APPENDED, never substituted: dropping the Markdown slot is not
    # one of the two legal arrays, which is the property clause 1 exists to hold.
    with pytest.raises(AssertionError, match="clause 1"):
        assert_formats_contract([RATIFIED_PDF_FORMAT])


@pytest.mark.parametrize(
    ("label", "formats_value", "clause"),
    [
        (
            "a third, unratified rendering",
            [
                {"format": "markdown", "filename": "report.md"},
                {"format": "pdf", "filename": "report.pdf"},
                {"format": "html", "filename": "report.html"},
            ],
            "clause 3",
        ),
        (
            "the Markdown slot renamed",
            [
                {"format": "markdown", "filename": "summary.md"},
                {"format": "pdf", "filename": "report.pdf"},
            ],
            "clause 1",
        ),
        (
            "a path-shaped appended filename",
            [
                {"format": "markdown", "filename": "report.md"},
                {"format": "pdf", "filename": "out/report.pdf"},
            ],
            "clause 4",
        ),
        (
            "an unratified format in place of the PDF entry",
            [
                {"format": "markdown", "filename": "report.md"},
                {"format": "docx", "filename": "report.docx"},
            ],
            "clause 3",
        ),
        (
            "an entry that is a bare string rather than an object",
            [
                {"format": "markdown", "filename": "report.md"},
                "pdf",
            ],
            "clause 4",
        ),
        (
            "an entry carrying a third key",
            [
                {"format": "markdown", "filename": "report.md"},
                {"format": "pdf", "filename": "report.pdf", "pages": 3},
            ],
            "clause 4",
        ),
        (
            "two appended entries, neither of them ratified",
            [
                {"format": "markdown", "filename": "report.md"},
                {"format": "pdf", "filename": "a.pdf"},
                {"format": "pdf", "filename": "b.pdf"},
            ],
            "clause 2",
        ),
    ],
)
def test_the_formats_contract_refuses_every_unratified_shape(
    label: str, formats_value: object, clause: str
) -> None:
    """The negative half: seven ways to be wrong, each refused by a NAMED clause.

    A rule stated only as "Markdown first, PDF optionally" passes the first four of
    these -- which is exactly why 8.1.1 enumerates clauses instead. Each case carries the
    clause it is meant to trip, so a future weakening of the rule fails HERE with a
    readable diff rather than silently admitting a shape.
    """
    with pytest.raises(AssertionError, match=clause):
        assert_formats_contract(formats_value)


def test_the_report_stage_knows_no_format_beyond_the_ratified_pair() -> None:
    """The production module's own vocabulary, not a copy of the test's.

    A rule that lives only in a test can be satisfied while production invents a fourth
    format string nobody ratified. This reads the literals out of the module itself, so
    the two cannot drift -- the same discipline 05-06 applied to chart text, where a
    hardcoded English word became a build failure instead of a review habit.
    """
    source = Path(build_report.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    declared = {
        entry["format"]
        for entry in RATIFIED_V1_FORMATS + [RATIFIED_PDF_FORMAT]
    }
    # Every format-looking literal the report stage writes must be ratified. The set
    # below is the RATIFIED set, so an unratified format fails here by name.
    unratified = {
        lit for lit in literals
        if lit in {"markdown", "pdf", "html", "docx", "csv", "json", "xml", "svg", "png"}
        and lit not in declared
    }
    assert not unratified, (
        f"the report stage writes unratified format names: {sorted(unratified)}; "
        f"report.v1 recognises {sorted(REPORT_V1_FORMATS)}"
    )
