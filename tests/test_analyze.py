"""End-to-end tests for the offline Wikipedia trend analyzer."""
from __future__ import annotations

import csv
import json
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
