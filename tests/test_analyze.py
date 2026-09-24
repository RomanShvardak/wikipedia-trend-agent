"""End-to-end tests for the offline Wikipedia trend analyzer."""
from __future__ import annotations

import ast
import csv
import json
import math
from datetime import date, timedelta
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
        "fasting-pl": (727, 728, 239.6, 0.0179, -37.5, -38.5, "medium", "noise"),
        "fasting-cs": (725, 728, 276.4, 0.0331, -54.3, -44.5, "medium", "noise"),
        "astronomy-uk": (1094, 1094, 1417.5, 0.0219, -56.0, -56.2, "medium", "down"),
        "space-exploration-uk": (1092, 1094, 545.8, 0.0247, -55.7, -57.7, "medium", "noise"),
        "english-pl": (728, 728, 8801.0, 0.0412, -18.6, -19.2, "medium", "down"),
        "english-cs": (728, 728, 3183.8, 0.0316, -22.3, -22.8, "medium", "down"),
        "english-uk": (728, 728, 8230.5, 0.0288, -36.2, -36.5, "medium", "down"),
        "english-pt": (728, 728, 10458.5, 0.0247, -24.7, -25.1, "high", "down"),
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
