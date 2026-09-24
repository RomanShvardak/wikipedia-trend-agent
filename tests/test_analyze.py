"""End-to-end tests for the offline Wikipedia trend analyzer."""
from __future__ import annotations

import csv
import json
import math
from datetime import date, timedelta
from pathlib import Path

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
