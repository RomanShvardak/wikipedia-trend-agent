"""End-to-end tests for the offline Wikipedia trend analyzer."""
from __future__ import annotations

import ast
import csv
import json
import math
import unicodedata
from datetime import date, timedelta
from fractions import Fraction
from pathlib import Path

import pytest

import analyze_trends


CSV_HEADER = ("date", "views", "series_id", "project", "article")


def _write_spec(tmp_path: Path, *, name: str = "one-series-tracer") -> Path:
    spec = {
        "name": name,
        "request": "Analyze one local series offline.",
        "language": "uk",
        "window": {"start": "20220101", "end": "20251230", "granularity": "daily"},
        "series": [
            {
                "id": "en-example",
                "project": "en.wikipedia",
                "article": "Example_article",
                "label": "Example article",
                "language": "en",
            }
        ],
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    return path


def _daily_rows(
    *,
    series_id: str = "en-example",
    project: str = "en.wikipedia",
    article: str = "Example_article",
    start: date = date(2022, 1, 1),
    days: int = 1460,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(days):
        current = start + timedelta(days=index)
        seasonal = (current.month % 4) * 5
        views = 1000 + (index // 30) * 4 + seasonal + (index % 7) * 2
        if index == 730:
            views *= 10
        rows.append(
            {
                "date": current.isoformat(),
                "views": views,
                "series_id": series_id,
                "project": project,
                "article": article,
            }
        )
    return rows


def _write_series_csv(path: Path, rows: list[dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADER)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _run_analyzer(spec_path: Path, out_dir: Path) -> int:
    return analyze_trends.main(["--spec", str(spec_path), "--out", str(out_dir)])


def test_one_series_reaches_finite_metrics_end_to_end(tmp_path: Path) -> None:
    """A local spec/CSV pair reaches the complete frozen metrics document offline."""
    spec_path = _write_spec(tmp_path)
    out_dir = tmp_path / "out"
    csv_path = _write_series_csv(out_dir / "series.csv", _daily_rows())

    assert _run_analyzer(spec_path, out_dir) == 0

    metrics_path = out_dir / "metrics.json"
    document = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert set(document) == {"spec_name", "as_of", "generated_from", "series"}
    assert document["spec_name"] == "one-series-tracer"
    assert document["as_of"] == "2025-12-30"
    assert document["generated_from"] == str(spec_path)
    assert len(document["series"]) == 1

    series = document["series"][0]
    assert set(series) == {
        "series_id",
        "project",
        "article",
        "label",
        "language",
        "total_views",
        "avg_daily_views",
        "period",
        "growth",
        "anomaly_share",
        "trend_direction",
        "confidence",
        "confidence_reasons",
        "seasonality",
        "anomalies",
    }
    assert series["series_id"] == "en-example"
    assert series["project"] == "en.wikipedia"
    assert series["article"] == "Example_article"
    assert series["period"] == {"start": "2022-01-01", "end": "2025-12-30", "days": 1460}
    assert set(series["growth"]) == {"m3", "y1", "y2"}
    assert any(anomaly["date"] == "2024-01-01" for anomaly in series["anomalies"])
    assert series["anomaly_share"] > 0.0
    assert series["trend_direction"] in {"up", "down", "flat", "noise", "inconclusive"}
    assert series["confidence"] in {"low", "medium", "high"}
    assert isinstance(series["confidence_reasons"], list)
    assert all(reason.strip() for reason in series["confidence_reasons"])
    assert isinstance(series["seasonality"], dict)
    assert set(series["seasonality"]) == {"months", "note"}
    assert all(isinstance(month, int) and 1 <= month <= 12 for month in series["seasonality"]["months"])

    def assert_finite(value: object) -> None:
        if isinstance(value, float):
            assert value == value and value not in (float("inf"), float("-inf"))
        elif isinstance(value, dict):
            for child in value.values():
                assert_finite(child)
        elif isinstance(value, list):
            for child in value:
                assert_finite(child)

    assert_finite(document)
    source = Path(analyze_trends.__file__).read_text(encoding="utf-8")
    assert "urllib" not in source
    assert "http" not in source.lower()


def _write_two_series_spec(tmp_path: Path) -> Path:
    spec = {
        "name": "two-series-spike-tracer",
        "request": "Compare two local series and isolate one viral day.",
        "language": "uk",
        "window": {"start": "20220101", "end": "20251230", "granularity": "daily"},
        "series": [
            {
                "id": "z-series",
                "project": "en.wikipedia",
                "article": "Z_series",
                "label": "Z series",
                "language": "en",
            },
            {
                "id": "a-series",
                "project": "uk.wikipedia",
                "article": "A_series",
                "label": "A series",
                "language": "uk",
            },
        ],
    }
    path = tmp_path / "two-series-spec.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    return path


def _two_series_rows(*, inject_spike: bool) -> tuple[list[dict[str, object]], int]:
    start = date(2022, 1, 1)
    seasonal = (0, 2, -1, 1, -2, 3, 0)
    spike_index = 1106
    rows_by_id: dict[str, list[dict[str, object]]] = {"a-series": [], "z-series": []}
    for index in range(1460):
        current = start + timedelta(days=index)
        offset = seasonal[index % len(seasonal)]
        z_views = 1000 + (index // 30) * 2 + offset
        a_views = 3000 - (index // 30) + offset
        if inject_spike and index == spike_index:
            z_views *= 10
        z_row = {
            "date": current.isoformat(),
            "views": z_views,
            "series_id": "z-series",
            "project": "en.wikipedia",
            "article": "Z_series",
        }
        a_row = {
            "date": current.isoformat(),
            "views": a_views,
            "series_id": "a-series",
            "project": "uk.wikipedia",
            "article": "A_series",
        }
        if current != date(2023, 6, 1):
            rows_by_id["z-series"].append(z_row)
        rows_by_id["a-series"].append(a_row)
    return rows_by_id["a-series"] + rows_by_id["z-series"], len(rows_by_id["z-series"])


def _assert_all_finite(value: object) -> None:
    if isinstance(value, float):
        assert math.isfinite(value)
    elif isinstance(value, dict):
        for child in value.values():
            _assert_all_finite(child)
    elif isinstance(value, list):
        for child in value:
            _assert_all_finite(child)


def test_two_series_preserves_spec_order_and_one_spike_is_median_replaced(
    tmp_path: Path,
) -> None:
    """Two series retain spec order and median replacement absorbs one viral day."""
    spec_path = _write_two_series_spec(tmp_path)
    before_dir = tmp_path / "before"
    after_dir = tmp_path / "after"
    before_rows, before_z_count = _two_series_rows(inject_spike=False)
    after_rows, after_z_count = _two_series_rows(inject_spike=True)
    _write_series_csv(before_dir / "series.csv", before_rows)
    _write_series_csv(after_dir / "series.csv", after_rows)

    assert _run_analyzer(spec_path, before_dir) == 0
    assert _run_analyzer(spec_path, after_dir) == 0

    before = json.loads((before_dir / "metrics.json").read_text(encoding="utf-8"))
    after = json.loads((after_dir / "metrics.json").read_text(encoding="utf-8"))
    _assert_all_finite(before)
    _assert_all_finite(after)
    assert [item["series_id"] for item in before["series"]] == ["z-series", "a-series"]
    assert [item["series_id"] for item in after["series"]] == ["z-series", "a-series"]

    before_z, after_z = before["series"][0], after["series"][0]
    assert before_z["period"] == after_z["period"]
    assert before_z["anomalies"] == []
    assert [item["date"] for item in after_z["anomalies"]] == ["2025-01-11"]
    assert after_z["anomaly_share"] == 1 / after_z_count
    assert before_z_count == after_z_count
    assert set(before_z["growth"]) == {"m3", "y1", "y2"}
    assert json.dumps(before_z["growth"]["y1"]["clean"], sort_keys=True) == json.dumps(
        after_z["growth"]["y1"]["clean"], sort_keys=True
    )
    assert before_z["trend_direction"] == after_z["trend_direction"]


def _observation(
    day: date, views: int = 100, *, series_id: str = "growth-series"
) -> analyze_trends.Observation:
    return analyze_trends.Observation(
        date=day,
        views=views,
        series_id=series_id,
        project="en.wikipedia",
        article="Growth_series",
    )


def _constant_observations(start: date, count: int, views: int = 100) -> list[analyze_trends.Observation]:
    return [_observation(start + timedelta(days=index), views) for index in range(count)]


@pytest.mark.parametrize(
    ("days", "start", "previous_start", "previous_end"),
    [
        (91, date(2024, 1, 1), date(2023, 10, 2), date(2023, 12, 31)),
        (365, date(2023, 4, 2), date(2022, 4, 2), date(2023, 4, 1)),
        (730, date(2022, 4, 2), date(2020, 4, 2), date(2022, 4, 1)),
    ],
)
def test_growth_windows_use_inclusive_equal_calendar_ranges(
    days: int,
    start: date,
    previous_start: date,
    previous_end: date,
) -> None:
    end = start + timedelta(days=days - 1)
    observations = _constant_observations(previous_start, days * 2)

    result = analyze_trends.compute_growth_for_days(observations, end, days)

    assert result["start"] == start.isoformat()
    assert result["end"] == end.isoformat()
    assert result["previous_start"] == previous_start.isoformat()
    assert result["previous_end"] == previous_end.isoformat()
    assert result["pct"] == 0.0
    assert result["clean"]["pct"] == 0.0


@pytest.mark.parametrize(("days", "floor"), [(91, 73), (365, 292), (730, 584)])
def test_growth_coverage_floor_boundaries(days: int, floor: int) -> None:
    start = date(2020, 1, 1)
    end = start + timedelta(days=days * 2 - 1)
    current_start = end - timedelta(days=days - 1)
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=days - 1)
    all_days = _constant_observations(previous_start, days * 2)

    below_floor = [
        observation
        for observation in all_days
        if previous_start <= observation.date <= previous_end
    ][: floor - 1] + [
        observation
        for observation in all_days
        if current_start <= observation.date <= end
    ][: floor - 1]
    below_result = analyze_trends.compute_growth_for_days(below_floor, end, days)
    assert below_result["pct"] is None
    assert below_result["clean"]["pct"] is None
    assert below_result["reason"] == analyze_trends.INSUFFICIENT_OBSERVATIONS_REASON
    assert below_result["clean"]["reason"] == analyze_trends.INSUFFICIENT_OBSERVATIONS_REASON

    exact_floor = [
        observation
        for observation in all_days
        if previous_start <= observation.date <= previous_end
    ][:floor] + [
        observation
        for observation in all_days
        if current_start <= observation.date <= end
    ][:floor]
    exact_result = analyze_trends.compute_growth_for_days(exact_floor, end, days)
    assert len([row for row in exact_floor if current_start <= row.date <= end]) == floor
    assert len([row for row in exact_floor if previous_start <= row.date <= previous_end]) == floor
    assert exact_result["pct"] == 0.0
    assert exact_result["clean"]["pct"] == 0.0


def test_growth_preserves_numeric_zero_and_previous_zero_null() -> None:
    end = date(2024, 3, 31)
    unchanged = _constant_observations(date(2022, 4, 2), 730)
    assert analyze_trends.compute_growth_for_days(unchanged, end, 365)["pct"] == 0.0

    previous_zero = _constant_observations(date(2022, 4, 2), 365, views=0)
    current = _constant_observations(date(2023, 4, 2), 365, views=100)
    result = analyze_trends.compute_growth_for_days(previous_zero + current, end, 365)
    assert result["pct"] is None
    assert result["abs"] is None
    assert result["reason"] == analyze_trends.ZERO_PREVIOUS_MEAN_REASON
    assert result["clean"]["pct"] is None
    assert result["clean"]["reason"] == analyze_trends.ZERO_PREVIOUS_MEAN_REASON


def test_anomaly_detector_handles_short_zero_mad_and_date_windows() -> None:
    short = _constant_observations(date(2024, 1, 1), 13)
    assert analyze_trends.detect_anomalies(short) == []

    zero_mad = _constant_observations(date(2024, 1, 1), 20)
    assert analyze_trends.detect_anomalies(zero_mad) == []

    rows = [
        _observation(date(2024, 1, 1) + timedelta(days=index), 100 + (index % 3) - 1)
        for index in range(20)
    ]
    rows[9] = _observation(rows[9].date, 1000)
    found = analyze_trends.detect_anomalies(rows)
    assert any(item["date"] == "2024-01-10" for item in found)


def test_anomaly_share_uses_observed_days(tmp_path: Path) -> None:
    spec_path = _write_spec(tmp_path)
    out_dir = tmp_path / "out"
    rows = _daily_rows()
    rows = [row for row in rows if row["date"] != "2023-06-01"]
    _write_series_csv(out_dir / "series.csv", rows)

    assert _run_analyzer(spec_path, out_dir) == 0
    document = json.loads((out_dir / "metrics.json").read_text(encoding="utf-8"))
    series = document["series"][0]
    assert series["anomaly_share"] == len(series["anomalies"]) / len(rows)


def test_injected_spike_does_not_flip_direction(tmp_path: Path) -> None:
    spec_path = _write_spec(tmp_path)
    before_dir = tmp_path / "before"
    after_dir = tmp_path / "after"
    before_rows = [
        {**row, "views": row["views"] // 10} if row["date"] == "2024-01-01" else row
        for row in _daily_rows()
    ]
    after_rows = [
        {**row, "views": row["views"] * 10} if row["date"] == "2024-01-01" else row
        for row in before_rows
    ]
    _write_series_csv(before_dir / "series.csv", before_rows)
    _write_series_csv(after_dir / "series.csv", after_rows)
    assert _run_analyzer(spec_path, before_dir) == 0
    assert _run_analyzer(spec_path, after_dir) == 0
    before = json.loads((before_dir / "metrics.json").read_text(encoding="utf-8"))["series"][0]
    after = json.loads((after_dir / "metrics.json").read_text(encoding="utf-8"))["series"][0]
    assert before["growth"]["y1"]["clean"] == after["growth"]["y1"]["clean"]
    assert before["trend_direction"] == after["trend_direction"]


def test_growth_rejects_malformed_or_oversized_csv_before_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path = _write_spec(tmp_path)
    out_dir = tmp_path / "out"
    _write_series_csv(out_dir / "series.csv", _daily_rows())
    assert _run_analyzer(spec_path, out_dir) == 0
    sentinel = out_dir / "metrics.json"
    sentinel.write_text("sentinel", encoding="utf-8")
    monkeypatch.setattr(
        analyze_trends,
        "build_metrics",
        lambda *args: (_ for _ in ()).throw(AssertionError("statistics must not run")),
    )

    malformed = tmp_path / "malformed"
    malformed.mkdir()
    (malformed / "series.csv").write_text("wrong,header\n", encoding="utf-8")
    assert analyze_trends.main(["--spec", str(spec_path), "--out", str(malformed)]) == 1

    oversized = tmp_path / "oversized"
    oversized.mkdir()
    (oversized / "series.csv").write_text(
        "date,views,series_id,project,article\n" + ("x" * 64), encoding="utf-8"
    )
    monkeypatch.setattr(analyze_trends, "MAX_CSV_BYTES", 16)
    assert analyze_trends.main(["--spec", str(spec_path), "--out", str(oversized)]) == 1
    assert sentinel.read_text(encoding="utf-8") == "sentinel"


def test_growth_candidate_metrics_reject_non_finite_values_before_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    spec_path = _write_spec(tmp_path)
    out_dir = tmp_path / "out"
    _write_series_csv(out_dir / "series.csv", _daily_rows())
    monkeypatch.setattr(
        analyze_trends,
        "build_metrics",
        lambda *args: {"series": [{"value": float("nan")}]},
    )
    called = False

    def unexpected_dump(*args: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(analyze_trends, "dump_json", unexpected_dump)
    assert analyze_trends.main(["--spec", str(spec_path), "--out", str(out_dir)]) == 1
    assert not called
    assert "non-finite" in capsys.readouterr().err


def test_growth_identity_and_partial_input_fail_without_replacing_metrics(tmp_path: Path) -> None:
    spec_path = _write_spec(tmp_path)
    out_dir = tmp_path / "out"
    rows = _daily_rows()
    rows[0] = {**rows[0], "article": "Tampered_article"}
    _write_series_csv(out_dir / "series.csv", rows)
    sentinel = out_dir / "metrics.json"
    sentinel.write_text("sentinel", encoding="utf-8")
    assert _run_analyzer(spec_path, out_dir) == 1
    assert sentinel.read_text(encoding="utf-8") == "sentinel"

    partial_spec = _write_two_series_spec(tmp_path / "partial")
    partial_out = tmp_path / "partial-out"
    _write_series_csv(partial_out / "series.csv", _daily_rows())
    partial_sentinel = partial_out / "metrics.json"
    partial_sentinel.write_text("sentinel", encoding="utf-8")
    assert _run_analyzer(partial_spec, partial_out) == 1
    assert partial_sentinel.read_text(encoding="utf-8") == "sentinel"


def test_growth_source_has_no_dynamic_execution_path() -> None:
    source = Path(analyze_trends.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_calls = {"eval", "exec", "compile", "__import__"}
    forbidden_imports = {"subprocess", "pickle", "multiprocessing", "runpy"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in forbidden_calls
        if isinstance(node, ast.Import):
            assert not {alias.name.split(".")[0] for alias in node.names} & forbidden_imports
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in forbidden_imports


def test_threshold_fixture_matches_frozen_constants() -> None:
    fixture = json.loads(
        (Path(__file__).resolve().parent / "fixtures" / "analysis.threshold-validation.json").read_text(
            encoding="utf-8"
        )
    )
    assert fixture["algorithm_version"] == "rolling-median-mad-v1"
    assert fixture["source_date"] == "2026-09-24"
    assert fixture["mad_k"] == analyze_trends.MAD_K
    assert fixture["mad_scale"] == analyze_trends.MAD_SCALE
    assert fixture["coverage_ratio"] == analyze_trends.COVERAGE_RATIO
    assert fixture["direction_band_pct"] == analyze_trends.DIRECTION_BAND_PCT
    assert fixture["minimum_monthly_views"] == analyze_trends.MIN_MONTHLY_VIEWS
    assert fixture["maximum_reliable_anomaly_share"] == analyze_trends.ANOMALY_SHARE_BOUNDARY
    assert len(fixture["series"]) == 8


def test_threshold_fixture_summaries_remain_inside_calibrated_bands() -> None:
    fixture = json.loads(
        (Path(__file__).resolve().parent / "fixtures" / "analysis.threshold-validation.json").read_text(
            encoding="utf-8"
        )
    )
    expected = {
        "fasting-pl": (727, 728, 239.6, 0.0179, -37.5, -38.5, "low", "noise"),
        "fasting-cs": (725, 728, 276.4, 0.0331, -54.3, -44.5, "low", "noise"),
        "astronomy-uk": (1094, 1094, 1417.5, 0.0219, -56.0, -56.2, "medium", "down"),
        "space-exploration-uk": (1092, 1094, 545.8, 0.0247, -55.7, -57.7, "medium", "noise"),
        "english-pl": (728, 728, 8801.0, 0.0412, -18.6, -19.2, "medium", "down"),
        "english-cs": (728, 728, 3183.8, 0.0316, -22.3, -22.8, "medium", "down"),
        "english-uk": (728, 728, 8230.5, 0.0288, -36.2, -36.5, "medium", "down"),
        "english-pt": (728, 728, 10458.5, 0.0247, -24.7, -25.1, "medium", "down"),
    }
    assert {item["id"] for item in fixture["series"]} == set(expected)
    for item in fixture["series"]:
        summary = expected[item["id"]]
        actual = (
            item["rows"],
            item["span_days"],
            item["monthly_30d"],
            item["anomaly_share"],
            item["raw_y1_pct"],
            item["clean_y1_pct"],
            item["confidence"],
            item["direction"],
        )
        assert actual == summary
        _assert_all_finite(item)
        assert 0.0 <= item["anomaly_share"] < analyze_trends.ANOMALY_SHARE_BOUNDARY
        assert item["monthly_30d"] >= 0.0
        assert analyze_trends.safe_direction(
            item["span_days"],
            item["raw_y1_pct"],
            item["clean_y1_pct"],
            item["confidence"],
            item["monthly_30d"],
        ) == item["direction"]
    

def _profile_rows(
    start: date,
    count: int,
    *,
    base_views: int,
    seasonal_months: set[int] | None = None,
) -> list[dict[str, object]]:
    seasonal_months = seasonal_months or set()
    rows: list[dict[str, object]] = []
    for index in range(count):
        current = start + timedelta(days=index)
        views = base_views + (200 if current.month in seasonal_months else 0)
        rows.append(
            {
                "date": current.isoformat(),
                "views": views,
                "series_id": "en-example",
                "project": "en.wikipedia",
                "article": "Example_article",
            }
        )
    return rows


def _profile_observations(rows: list[dict[str, object]]) -> list[analyze_trends.Observation]:
    observations: list[analyze_trends.Observation] = []
    for row in rows:
        date_text = row["date"]
        views = row["views"]
        assert isinstance(date_text, str)
        assert isinstance(views, int)
        observations.append(
            analyze_trends.Observation(
                date=date.fromisoformat(date_text),
                views=views,
                series_id="s",
                project="en.wikipedia",
                article="S",
            )
        )
    return observations


def test_confidence_and_seasonality_profiles_emit_safe_truthful_metrics(
    tmp_path: Path,
) -> None:
    """Evidence profiles keep seasonal context separate from safe direction."""
    spec_path = _write_spec(tmp_path)
    high_out = tmp_path / "high"
    high_rows = _profile_rows(
        date(2024, 1, 1), 730, base_views=10_000, seasonal_months={3, 9}
    )
    _write_series_csv(high_out / "series.csv", high_rows)
    assert _run_analyzer(spec_path, high_out) == 0
    high = json.loads((high_out / "metrics.json").read_text(encoding="utf-8"))["series"][0]
    assert high["confidence"] == "high"
    assert high["seasonality"]["months"] == [3, 9]
    assert high["trend_direction"] != "inconclusive"

    ninety_out = tmp_path / "ninety"
    _write_series_csv(ninety_out / "series.csv", _profile_rows(date(2024, 1, 1), 90, base_views=10))
    assert _run_analyzer(spec_path, ninety_out) == 0
    ninety = json.loads((ninety_out / "metrics.json").read_text(encoding="utf-8"))["series"][0]
    assert ninety["confidence"] == "low"
    assert analyze_trends.PERIOD_BELOW_MINIMUM_REASON in ninety["confidence_reasons"]
    assert "low confidence: treat the reading as a hypothesis" in ninety["confidence_reasons"]
    assert ninety["trend_direction"] == "inconclusive"
    assert ninety["seasonality"]["months"] == []

    one_out = tmp_path / "one"
    _write_series_csv(one_out / "series.csv", _profile_rows(date(2024, 1, 1), 1, base_views=10))
    assert _run_analyzer(spec_path, one_out) == 0
    one = json.loads((one_out / "metrics.json").read_text(encoding="utf-8"))["series"][0]
    assert all(window["pct"] is None for window in one["growth"].values())
    assert all(window["reason"] for window in one["growth"].values())
    assert one["confidence"] == "low"
    assert "low confidence: treat the reading as a hypothesis" in one["confidence_reasons"]
    assert one["trend_direction"] == "inconclusive"
    assert one["seasonality"]["months"] == []


def test_seasonality_requires_two_aligned_halves_and_matches_repeating_months() -> None:
    """Seasonality requires complete aligned halves and matches months in both."""
    end = date(2025, 12, 30)
    complete = _profile_rows(date(2024, 1, 1), 730, base_views=100, seasonal_months={4, 10})
    assert analyze_trends.compute_seasonality(
        _profile_observations(complete), end
    )["months"] == [4, 10]
    incomplete = _profile_observations(complete[:364])
    result = analyze_trends.compute_seasonality(incomplete, date(2024, 12, 30))
    assert result["months"] == []
    assert result["note"] == analyze_trends.SEASONALITY_UNAVAILABLE_NOTE


def test_minimum_period_and_confidence_score_boundaries() -> None:
    """Confidence points and the 90/91 inclusive boundary follow the frozen rubric."""
    level, reasons = analyze_trends.score_confidence(90, 0, 0, False, None)
    assert level == "low"
    assert analyze_trends.PERIOD_BELOW_MINIMUM_REASON in reasons
    assert "low confidence: treat the reading as a hypothesis" in reasons
    assert analyze_trends.score_confidence(91, 999, 0, False, None)[0] == "medium"
    assert analyze_trends.score_confidence(730, 10_000, 0, True, None)[0] == "high"
    assert analyze_trends.score_confidence(90, 10_000, 0.01, True, None)[0] == "medium"


def test_methodology_penalty_applies_once_to_comparison_span() -> None:
    """The methodology penalty follows the actual y1 comparison span."""
    def observations(start: date, count: int) -> list[analyze_trends.Observation]:
        return _constant_observations(start, count, views=100)

    crossing = observations(date(2014, 5, 1), 730)
    assert analyze_trends.comparison_crosses_methodology_break(crossing, date(2016, 4, 30))
    after = observations(date(2016, 5, 1), 730)
    assert not analyze_trends.comparison_crosses_methodology_break(after, date(2018, 4, 30))
    before = observations(date(2012, 5, 1), 730)
    assert not analyze_trends.comparison_crosses_methodology_break(before, date(2014, 4, 30))
    level, reasons = analyze_trends.score_confidence(
        730, 10_000, 0, True, (date(2014, 5, 1), date(2016, 4, 30))
    )
    assert level == "medium"
    assert analyze_trends.METHODOLOGY_CROSSING_REASON in reasons
    assert reasons.count(analyze_trends.METHODOLOGY_CROSSING_REASON) == 1


def test_direction_gate_hierarchy_and_hypothesis_reason() -> None:
    """Direction checks are ordered and never expose unsupported up/down."""
    assert analyze_trends.safe_direction(90, 50, 50, "low", 10_000) == "inconclusive"
    assert analyze_trends.safe_direction(365, 50, 50, "low", 10_000) == "noise"
    assert analyze_trends.safe_direction(365, 50, 50, "high", 999) == "noise"
    assert analyze_trends.safe_direction(365, 10, 10, "high", 1_000) == "flat"
    assert analyze_trends.safe_direction(365, 10, 11, "high", 1_000) == "noise"
    assert analyze_trends.safe_direction(365, 11, 11, "high", 1_000) == "up"
    assert analyze_trends.safe_direction(365, -11, -11, "high", 1_000) == "down"


def test_empty_null_and_single_element_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty and malformed data fail, while one valid observation is explicit."""
    spec_path = _write_spec(tmp_path)
    empty_out = tmp_path / "empty"
    empty_out.mkdir()
    (empty_out / "series.csv").write_bytes(b"")
    empty_metrics = empty_out / "metrics.json"
    empty_metrics.write_text("sentinel", encoding="utf-8")
    assert _run_analyzer(spec_path, empty_out) == 1
    assert empty_metrics.read_text(encoding="utf-8") == "sentinel"

    invalid_utf8 = tmp_path / "invalid-utf8"
    invalid_utf8.mkdir()
    (invalid_utf8 / "series.csv").write_bytes(
        ",".join(CSV_HEADER).encode("utf-8") + b"\n2024-01-01,\xff,en-example,en.wikipedia,Example_article\n"
    )
    assert _run_analyzer(spec_path, invalid_utf8) == 1

    invalid_rows = [
        ("null", "en-example", "en.wikipedia", "Example_article"),
        ("1", "unknown", "en.wikipedia", "Example_article"),
        ("-1", "en-example", "en.wikipedia", "Example_article"),
    ]
    for index, (views, series_id, project, article) in enumerate(invalid_rows):
        out_dir = tmp_path / f"invalid-{index}"
        _write_series_csv(
            out_dir / "series.csv",
            [
                {
                    "date": "2024-01-01",
                    "views": views,
                    "series_id": series_id,
                    "project": project,
                    "article": article,
                }
            ],
        )
        assert _run_analyzer(spec_path, out_dir) == 1

    single_out = tmp_path / "single"
    _write_series_csv(
        single_out / "series.csv",
        [
            {
                "date": "2024-01-01",
                "views": 10,
                "series_id": "en-example",
                "project": "en.wikipedia",
                "article": "Example_article",
            }
        ],
    )
    assert _run_analyzer(spec_path, single_out) == 0
    single = json.loads((single_out / "metrics.json").read_text(encoding="utf-8"))["series"][0]
    assert single["period"]["days"] == 1
    assert all(window["pct"] is None and window["reason"] for window in single["growth"].values())
    assert single["confidence"] == "low"
    assert "low confidence: treat the reading as a hypothesis" in single["confidence_reasons"]
    assert single["trend_direction"] == "inconclusive"

    stream_out = tmp_path / "stream"
    stream_csv = _write_series_csv(stream_out / "series.csv", _profile_rows(date(2024, 1, 1), 2, base_views=10))
    original_read_text = Path.read_text

    def reject_csv_read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == stream_csv:
            raise AssertionError("CSV input must be parsed without read_text")
        return original_read_text(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", reject_csv_read_text)
    assert _run_analyzer(spec_path, stream_out) == 0


def test_identity_equality_uses_exact_utf8_code_points(tmp_path: Path) -> None:
    """Identity comparison does not normalize Unicode code points."""
    spec_path = _write_spec(tmp_path)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["series"][0]["article"] = "café"
    spec["series"][0]["label"] = "Café"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    out_dir = tmp_path / "out"
    rows = _profile_rows(date(2024, 1, 1), 1, base_views=10)
    rows[0]["article"] = unicodedata.normalize("NFD", "café")
    _write_series_csv(out_dir / "series.csv", rows)
    assert _run_analyzer(spec_path, out_dir) == 1
    metrics_path = out_dir / "metrics.json"
    metrics_path.write_text("sentinel", encoding="utf-8")
    assert _run_analyzer(spec_path, out_dir) == 1
    assert metrics_path.read_text(encoding="utf-8") == "sentinel"

    valid_out = tmp_path / "valid"
    valid_rows = _profile_rows(date(2024, 1, 1), 1, base_views=10)
    valid_rows[0]["article"] = "café"
    _write_series_csv(valid_out / "series.csv", valid_rows)
    assert _run_analyzer(spec_path, valid_out) == 0
    valid = json.loads((valid_out / "metrics.json").read_text(encoding="utf-8"))["series"][0]
    assert valid["article"] == "café"
    assert valid["label"] == "Café"


def test_partial_tampered_and_oversized_inputs_leave_metrics_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Partial, tampered, and capped CSV inputs cannot replace prior output."""
    spec_path = _write_spec(tmp_path)
    valid_rows = _profile_rows(date(2024, 1, 1), 2, base_views=10)
    partial_spec_path = _write_two_series_spec(tmp_path / "partial-spec")
    partial_out = tmp_path / "partial"
    _write_series_csv(partial_out / "series.csv", [_daily_rows()[0]])
    partial_metrics = partial_out / "metrics.json"
    partial_metrics.write_text("sentinel", encoding="utf-8")
    assert _run_analyzer(partial_spec_path, partial_out) == 1
    assert partial_metrics.read_text(encoding="utf-8") == "sentinel"

    for label, rows in (
        ("tampered", [{**valid_rows[0], "article": "Tampered"}, valid_rows[1]]),
        ("duplicate", [valid_rows[0], valid_rows[0]]),
    ):
        out_dir = tmp_path / label
        _write_series_csv(out_dir / "series.csv", rows)
        metrics_path = out_dir / "metrics.json"
        metrics_path.write_text("sentinel", encoding="utf-8")
        assert _run_analyzer(spec_path, out_dir) == 1
        assert metrics_path.read_text(encoding="utf-8") == "sentinel"

    over_rows = tmp_path / "oversized"
    _write_series_csv(over_rows / "series.csv", valid_rows + [valid_rows[1]])
    over_metrics = over_rows / "metrics.json"
    over_metrics.write_text("sentinel", encoding="utf-8")
    monkeypatch.setattr(analyze_trends, "MAX_OBSERVATIONS", 2)
    assert _run_analyzer(spec_path, over_rows) == 1
    assert over_metrics.read_text(encoding="utf-8") == "sentinel"

    byte_limited = tmp_path / "byte-limited"
    _write_series_csv(byte_limited / "series.csv", valid_rows)
    byte_metrics = byte_limited / "metrics.json"
    byte_metrics.write_text("sentinel", encoding="utf-8")
    monkeypatch.setattr(analyze_trends, "MAX_CSV_BYTES", 16)
    assert _run_analyzer(spec_path, byte_limited) == 1
    assert byte_metrics.read_text(encoding="utf-8") == "sentinel"


def test_metrics_values_are_finitely_serializable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI preflights candidates with strict allow_nan=False serialization."""
    spec_path = _write_spec(tmp_path)
    out_dir = tmp_path / "out"
    _write_series_csv(out_dir / "series.csv", _profile_rows(date(2024, 1, 1), 2, base_views=10))
    original_dumps = analyze_trends.json.dumps
    calls: list[dict[str, object]] = []

    def record_dumps(value: object, **kwargs: object) -> str:
        calls.append(kwargs)
        return original_dumps(value, **kwargs)

    monkeypatch.setattr(analyze_trends.json, "dumps", record_dumps)
    assert _run_analyzer(spec_path, out_dir) == 0
    assert any(kwargs.get("allow_nan") is False for kwargs in calls)
    with pytest.raises(analyze_trends.AnalysisError, match="non-finite"):
        analyze_trends.validate_finite_numbers({"nested": [float("inf")]})


def test_analyzer_uses_data_parsers_without_dynamic_execution() -> None:
    """The analyzer AST contains no dynamic execution or process-launch path."""
    tree = ast.parse(Path(analyze_trends.__file__).read_text(encoding="utf-8"))
    forbidden_calls = {"eval", "exec", "compile", "__import__", "system", "popen", "run"}
    forbidden_imports = {"subprocess", "pickle", "multiprocessing", "runpy", "os"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in forbidden_calls
        if isinstance(node, ast.Import):
            assert not {alias.name.split(".")[0] for alias in node.names} & forbidden_imports
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in forbidden_imports


def test_spec_window_rejects_out_of_range_dates(tmp_path: Path) -> None:
    """Only observations inside the validated inclusive spec window are accepted."""
    spec_path = _write_spec(tmp_path)
    valid_dates = (date(2022, 1, 1), date(2025, 12, 30))
    invalid_dates = (date(2021, 12, 31), date(2025, 12, 31))

    for observed_date in valid_dates:
        out_dir = tmp_path / f"valid-{observed_date.isoformat()}"
        _write_series_csv(
            out_dir / "series.csv",
            [
                {
                    "date": observed_date.isoformat(),
                    "views": 10,
                    "series_id": "en-example",
                    "project": "en.wikipedia",
                    "article": "Example_article",
                }
            ],
        )
        assert _run_analyzer(spec_path, out_dir) == 0

    for observed_date in invalid_dates:
        out_dir = tmp_path / f"invalid-{observed_date.isoformat()}"
        _write_series_csv(
            out_dir / "series.csv",
            [
                {
                    "date": observed_date.isoformat(),
                    "views": 10,
                    "series_id": "en-example",
                    "project": "en.wikipedia",
                    "article": "Example_article",
                }
            ],
        )
        metrics_path = out_dir / "metrics.json"
        metrics_path.write_bytes(b'{"sentinel":"preserve"}\n')
        assert _run_analyzer(spec_path, out_dir) == 1
        assert metrics_path.read_bytes() == b'{"sentinel":"preserve"}\n'


def test_views_cell_rejects_overlong_integers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exact 20-digit boundary is accepted and longer cells stay on the error path."""
    spec_path = _write_spec(tmp_path)
    for label, views, expected_status in (
        ("boundary", "9" * 20, 0),
        ("overlong", "9" * 5000, 1),
    ):
        out_dir = tmp_path / label
        _write_series_csv(
            out_dir / "series.csv",
            [
                {
                    "date": "2024-01-01",
                    "views": views,
                    "series_id": "en-example",
                    "project": "en.wikipedia",
                    "article": "Example_article",
                }
            ],
        )
        metrics_path = out_dir / "metrics.json"
        metrics_path.write_bytes(b"sentinel")
        status = 0
        try:
            status = _run_analyzer(spec_path, out_dir)
        except ValueError:
            status = 0
        assert status == expected_status
        if expected_status == 1:
            captured = capsys.readouterr()
            assert "analysis failed:" in captured.err
            assert f"row 2:" in captured.err
            assert metrics_path.read_bytes() == b"sentinel"
        else:
            assert json.loads(metrics_path.read_text(encoding="utf-8"))["series"]


def test_missing_seasonality_month_is_unavailable_above_row_floor() -> None:
    """A missing calendar month blocks seasonality even above the 80% row floor."""
    end = date(2025, 12, 30)
    observations = _constant_observations(date(2024, 1, 1), 730)
    previous_start = date(2024, 1, 1)
    previous_end = date(2024, 12, 31)
    missing_month = [row for row in observations if row.date.month != 2 and row.date <= previous_end]
    current_half = [row for row in observations if row.date > previous_end]
    assert len(missing_month) >= math.ceil(0.8 * 365)
    assert len(current_half) >= math.ceil(0.8 * 365)
    assert previous_start <= observations[0].date <= previous_end

    growth = analyze_trends.compute_growth_for_days(observations, end, 365)
    seasonality = analyze_trends.compute_seasonality(missing_month + current_half, end)

    assert growth["pct"] == 0.0
    assert seasonality == {
        "months": [],
        "note": analyze_trends.SEASONALITY_UNAVAILABLE_NOTE,
    }


def test_metrics_output_is_atomic_and_preserves_sentinel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A replace failure cleans staging and preserves the prior metrics bytes."""
    spec_path = _write_spec(tmp_path)
    out_dir = tmp_path / "out"
    _write_series_csv(out_dir / "series.csv", _profile_rows(date(2024, 1, 1), 2, base_views=10))
    metrics_path = out_dir / "metrics.json"
    sentinel = b'{"sentinel":"preserve"}\n'
    metrics_path.write_bytes(sentinel)
    staged_paths: list[Path] = []

    def fail_replace(source: object, destination: object) -> None:
        source_path = Path(str(source))
        staged_paths.append(source_path)
        assert source_path.parent == metrics_path.parent
        assert source_path.exists()
        assert Path(str(destination)) == metrics_path
        raise OSError("injected atomic replace failure")

    monkeypatch.setattr(analyze_trends.common.os, "replace", fail_replace)

    assert _run_analyzer(spec_path, out_dir) == 1
    captured = capsys.readouterr()

    assert staged_paths
    assert "analysis failed:" in captured.err
    assert "metrics output" in captured.err
    assert metrics_path.read_bytes() == sentinel
    assert {path.name for path in out_dir.iterdir()} == {"series.csv", "metrics.json"}


def test_committed_analysis_pair_matches_golden_per_series() -> None:
    """The committed spec/series pair regenerates every golden series exactly."""
    fixtures = Path(__file__).resolve().parent / "fixtures"
    spec_path = fixtures / "spec.example.json"
    series_path = fixtures / "series.example.csv"
    assert series_path.is_file(), "committed analyzer input series.example.csv is required"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    golden = json.loads(
        (fixtures / "metrics.example.json").read_text(encoding="utf-8")
    )

    grouped = analyze_trends.load_series_csv(series_path, spec)
    actual = analyze_trends.build_metrics(
        spec, "tests/fixtures/spec.example.json", grouped
    )

    assert actual["series"] == golden["series"]
    assert actual["spec_name"] == golden["spec_name"]
    assert actual["as_of"] == golden["as_of"]
    assert actual["generated_from"] == golden["generated_from"]


def test_threshold_fixture_confidence_matches_production_score() -> None:
    """Every calibration label is produced by the frozen production scorer."""
    fixture = json.loads(
        (Path(__file__).resolve().parent / "fixtures" / "analysis.threshold-validation.json").read_text(
            encoding="utf-8"
        )
    )

    for item in fixture["series"]:
        confidence, _ = analyze_trends.score_confidence(
            item["span_days"],
            item["monthly_30d"],
            item["anomaly_share"],
            item["clean_y1_pct"] is not None,
            None,
        )
        assert item["confidence"] == confidence, item["id"]


def test_committed_golden_has_no_unreachable_seasonality_note() -> None:
    """Complete committed input never carries the unavailable-seasonality note."""
    golden = json.loads(
        (Path(__file__).resolve().parent / "fixtures" / "metrics.example.json").read_text(
            encoding="utf-8"
        )
    )
    reachable_notes = {
        analyze_trends.SEASONALITY_NO_PEAKS_NOTE,
        analyze_trends.SEASONALITY_AVAILABLE_NOTE,
    }

    for series in golden["series"]:
        assert series["seasonality"]["note"] in reachable_notes
        assert series["seasonality"]["note"] != analyze_trends.SEASONALITY_UNAVAILABLE_NOTE


def _write_precision_gap_spec(tmp_path: Path) -> Path:
    spec = {
        "name": "exact-growth-volume-tracer",
        "request": "Verify exact growth and exact monthly-volume eligibility.",
        "language": "uk",
        "window": {"start": "20220101", "end": "20251230", "granularity": "daily"},
        "series": [
            {
                "id": "large-count",
                "project": "en.wikipedia",
                "article": "Large_count",
                "label": "Large count",
                "language": "en",
            },
            {
                "id": "exact-volume",
                "project": "en.wikipedia",
                "article": "Exact_volume",
                "label": "Exact volume",
                "language": "en",
            },
        ],
    }
    path = tmp_path / "precision-gap-spec.json"
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    return path


def _precision_gap_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    large_start = date(2024, 1, 1)
    for index in range(730):
        rows.append(
            {
                "date": (large_start + timedelta(days=index)).isoformat(),
                "views": 10**19 + (1 if index >= 365 else 0),
                "series_id": "large-count",
                "project": "en.wikipedia",
                "article": "Large_count",
            }
        )

    volume_start = date(2022, 9, 24)
    for index in range(1_000):
        rows.append(
            {
                "date": (volume_start + timedelta(days=index)).isoformat(),
                "views": 33 if index < 665 else 34,
                "series_id": "exact-volume",
                "project": "en.wikipedia",
                "article": "Exact_volume",
            }
        )
    return rows


def test_exact_growth_and_unrounded_volume_end_to_end(tmp_path: Path) -> None:
    """One offline CLI run preserves exact large-count growth and volume gates."""
    spec_path = _write_precision_gap_spec(tmp_path)
    out_dir = tmp_path / "out"
    _write_series_csv(out_dir / "series.csv", _precision_gap_rows())

    assert _run_analyzer(spec_path, out_dir) == 0

    document = json.loads((out_dir / "metrics.json").read_text(encoding="utf-8"))
    _assert_all_finite(document)
    assert set(document) == {"spec_name", "as_of", "generated_from", "series"}
    assert [series["series_id"] for series in document["series"]] == [
        "large-count",
        "exact-volume",
    ]
    large_count, exact_volume = document["series"]

    assert large_count["growth"]["y1"]["abs"] == 1
    assert large_count["growth"]["y1"]["clean"]["abs"] == 1
    assert large_count["growth"]["y1"]["pct"] == 0.0
    assert large_count["growth"]["y1"]["clean"]["pct"] == 0.0

    monthly_30d_exact = Fraction(exact_volume["total_views"], exact_volume["period"]["days"]) * 30
    assert monthly_30d_exact == Fraction(20_001, 20)
    assert exact_volume["avg_daily_views"] == 33.3
    expected_confidence, _ = analyze_trends.score_confidence(
        exact_volume["period"]["days"],
        monthly_30d_exact,
        exact_volume["anomaly_share"],
        True,
        None,
    )
    expected_direction = analyze_trends.safe_direction(
        exact_volume["period"]["days"],
        exact_volume["growth"]["y1"]["pct"],
        exact_volume["growth"]["y1"]["clean"]["pct"],
        expected_confidence,
        monthly_30d_exact,
    )
    assert exact_volume["confidence"] == expected_confidence
    assert exact_volume["trend_direction"] == expected_direction
    assert analyze_trends.VOLUME_MINIMUM_REASON in exact_volume["confidence_reasons"]
    assert analyze_trends.VOLUME_BELOW_MINIMUM_REASON not in exact_volume["confidence_reasons"]


SEASONALITY_CONTROL_START = date(2024, 1, 1)
SEASONALITY_CONTROL_DAYS = 730
SEASONALITY_CONTROL_AMPLITUDE = 200
SEASONALITY_CONTROL_WINDOW = ("20240101", "20251230")


def _seasonality_control_rows(
    start: date,
    count: int,
    base_views: int,
    trend_step: int,
    seasonal_months: set[int],
    seasonal_amplitude: int = SEASONALITY_CONTROL_AMPLITUDE,
) -> list[analyze_trends.Observation]:
    rows: list[analyze_trends.Observation] = []
    for index in range(count):
        observed_date = start + timedelta(days=index)
        seasonal_effect = (
            seasonal_amplitude if observed_date.month in seasonal_months else 0
        )
        rows.append(
            _observation(
                observed_date,
                base_views + index * trend_step + seasonal_effect,
            )
        )
    return rows


def test_seasonality_separates_monotonic_trend_from_repeating_month_effects() -> None:
    """Production seasonality removes level changes but retains repeated calendar effects."""
    end = date(2025, 12, 30)
    previous_window, _ = analyze_trends._aligned_year_windows([], end)
    monotonic = _seasonality_control_rows(
        SEASONALITY_CONTROL_START, SEASONALITY_CONTROL_DAYS, 1_000, 1, set()
    )

    assert analyze_trends.compute_seasonality(monotonic, end) == {
        "months": [],
        "note": analyze_trends.SEASONALITY_NO_PEAKS_NOTE,
    }

    repeating = _seasonality_control_rows(
        SEASONALITY_CONTROL_START, SEASONALITY_CONTROL_DAYS, 1_000, 1, {4, 10}
    )
    assert analyze_trends.compute_seasonality(repeating, end) == {
        "months": [4, 10],
        "note": analyze_trends.SEASONALITY_AVAILABLE_NOTE,
    }

    missing_month = [
        observation
        for observation in repeating
        if not (
            previous_window[0] <= observation.date <= previous_window[1]
            and observation.date.month == 2
        )
    ]
    assert analyze_trends.compute_seasonality(missing_month, end) == {
        "months": [],
        "note": analyze_trends.SEASONALITY_UNAVAILABLE_NOTE,
    }


def test_committed_monotonic_golden_has_no_recurring_peaks() -> None:
    """The committed input is strictly monotonic and regenerates no-peak goldens."""
    fixtures = Path(__file__).resolve().parent / "fixtures"
    spec_path = fixtures / "spec.example.json"
    series_path = fixtures / "series.example.csv"
    golden = json.loads(
        (fixtures / "metrics.example.json").read_text(encoding="utf-8")
    )
    spec = json.loads(spec_path.read_text(encoding="utf-8"))

    with series_path.open(newline="", encoding="utf-8") as handle:
        csv_rows = list(csv.DictReader(handle))
    for series_id in (item["id"] for item in spec["series"]):
        values = [int(row["views"]) for row in csv_rows if row["series_id"] == series_id]
        assert values
        assert all(current > previous for previous, current in zip(values, values[1:]))

    grouped = analyze_trends.load_series_csv(series_path, spec)
    actual = analyze_trends.build_metrics(
        spec, "tests/fixtures/spec.example.json", grouped
    )
    assert actual == golden

    end = date(2026, 9, 20)
    for series_id, observations in grouped.items():
        assert analyze_trends.detect_anomalies(observations) == []
        assert analyze_trends.compute_seasonality(observations, end) == {
            "months": [],
            "note": analyze_trends.SEASONALITY_NO_PEAKS_NOTE,
        }, series_id

    for series in golden["series"]:
        assert series["seasonality"] == {
            "months": [],
            "note": analyze_trends.SEASONALITY_NO_PEAKS_NOTE,
        }


def _control_series_item(series_id: str, article: str) -> dict[str, str]:
    return {
        "id": series_id,
        "project": "en.wikipedia",
        "article": article,
        "label": series_id,
        "language": "en",
    }


def _write_control_spec(
    tmp_path: Path, *, name: str, series: list[dict[str, str]]
) -> Path:
    spec = {
        "name": name,
        "request": "Verify recurring-season context offline.",
        "language": "uk",
        "window": {
            "start": SEASONALITY_CONTROL_WINDOW[0],
            "end": SEASONALITY_CONTROL_WINDOW[1],
            "granularity": "daily",
        },
        "series": series,
    }
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    return path


def _control_base_views(shape: str, index: int, days: int) -> int:
    """Return the exact daily level of one strictly increasing control shape."""
    if shape == "linear":
        return 1_000 + index
    if shape == "concave":
        return 1_000_000 - (days - index) ** 2
    if shape == "convex":
        return 1_000 + index ** 2
    raise AssertionError(f"unknown monotonic control shape: {shape}")


def _control_rows(
    *,
    series_id: str,
    article: str,
    shape: str,
    effect_months: frozenset[int] = frozenset(),
    start: date = SEASONALITY_CONTROL_START,
    days: int = SEASONALITY_CONTROL_DAYS,
    amplitude: int = SEASONALITY_CONTROL_AMPLITUDE,
    drop_dates: frozenset[date] = frozenset(),
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(days):
        current = start + timedelta(days=index)
        if current in drop_dates:
            continue
        effect = amplitude if current.month in effect_months else 0
        rows.append(
            {
                "date": current.isoformat(),
                "views": _control_base_views(shape, index, days) + effect,
                "series_id": series_id,
                "project": "en.wikipedia",
                "article": article,
            }
        )
    return rows


def _february_dates_in(window: tuple[date, date]) -> frozenset[date]:
    start, end = window
    return frozenset(
        start + timedelta(days=index)
        for index in range((end - start).days + 1)
        if (start + timedelta(days=index)).month == 2
    )


FROZEN_SERIES_KEYS = {
    "series_id",
    "project",
    "article",
    "label",
    "language",
    "total_views",
    "avg_daily_views",
    "period",
    "growth",
    "anomaly_share",
    "trend_direction",
    "confidence",
    "confidence_reasons",
    "seasonality",
    "anomalies",
}


def test_seasonality_cli_retains_boundary_effects_and_removes_nonlinear_curvature(
    tmp_path: Path,
) -> None:
    """One production CLI run keeps January/December effects and drops concave curvature."""
    spec_path = _write_control_spec(
        tmp_path,
        name="boundary-nonlinear-tracer",
        series=[
            _control_series_item("boundary-series", "Boundary_series"),
            _control_series_item("concave-series", "Concave_series"),
        ],
    )
    out_dir = tmp_path / "out"
    rows = _control_rows(
        series_id="boundary-series",
        article="Boundary_series",
        shape="linear",
        effect_months=frozenset({1, 12}),
    )
    rows += _control_rows(
        series_id="concave-series",
        article="Concave_series",
        shape="concave",
    )
    _write_series_csv(out_dir / "series.csv", rows)

    assert _run_analyzer(spec_path, out_dir) == 0

    document = json.loads((out_dir / "metrics.json").read_text(encoding="utf-8"))
    assert set(document) == {"spec_name", "as_of", "generated_from", "series"}
    by_id = {series["series_id"]: series for series in document["series"]}
    assert sorted(by_id) == ["boundary-series", "concave-series"]
    for series in by_id.values():
        assert set(series) == FROZEN_SERIES_KEYS
        assert set(series["growth"]) == {"m3", "y1", "y2"}
        assert series["growth"]["y1"]["clean"]["pct"] is not None
        assert series["confidence_reasons"]
        assert series["seasonality"] == {
            "months": series["seasonality"]["months"],
            "note": series["seasonality"]["note"],
        }

    assert by_id["boundary-series"]["seasonality"] == {
        "months": [1, 12],
        "note": analyze_trends.SEASONALITY_AVAILABLE_NOTE,
    }
    assert by_id["concave-series"]["seasonality"] == {
        "months": [],
        "note": analyze_trends.SEASONALITY_NO_PEAKS_NOTE,
    }

    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    grouped = analyze_trends.load_series_csv(out_dir / "series.csv", spec)
    boundary_end = date.fromisoformat(by_id["boundary-series"]["period"]["end"])
    boundary_fit = analyze_trends._shared_month_effects(
        grouped["boundary-series"], boundary_end
    )
    assert boundary_fit is not None
    assert sorted(boundary_fit.shared_effects) == list(range(1, 13))
    for month in (1, 12):
        assert boundary_fit.shared_effects[month] > 0
        assert boundary_fit.pre_effect_residual[month][0] > 0
        assert boundary_fit.pre_effect_residual[month][1] > 0

    concave_end = date.fromisoformat(by_id["concave-series"]["period"]["end"])
    concave_fit = analyze_trends._shared_month_effects(
        grouped["concave-series"], concave_end
    )
    assert concave_fit is not None
    assert all(
        effect == 0 for effect in concave_fit.shared_effects.values()
    ), concave_fit.shared_effects


def test_solve_exact_normal_equations_rejects_singular_system() -> None:
    """A singular exact system is rejected before any selection evidence exists."""
    matrix = [[Fraction(1), Fraction(2)], [Fraction(2), Fraction(4)]]
    right_hand_side = [Fraction(1), Fraction(2)]

    with pytest.raises(
        analyze_trends.AnalysisError, match="singular|non-identifiable"
    ):
        analyze_trends._solve_exact_normal_equations(matrix, right_hand_side)
