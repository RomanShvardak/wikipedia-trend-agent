"""Offline tests for the Wikipedia trend chart stage.

Zero-network guarantee: this module never opens a socket — the chart stage reads
only committed fixtures plus the analyzer's own pure helpers, and every test
asserts the committed fixture bytes are unchanged after it runs.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from statistics import median
from typing import Any

import analyze_trends
import make_charts


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

TRACER_SERIES_ID = "pl-post-przerywany"
ALL_SERIES_IDS = ("cs-pust-prerusovany", "pl-post-przerywany")

COMMITTED_FIXTURES = (
    "series.anomalies.example.csv",
    "metrics.anomalies.example.json",
    "series.gaps.example.csv",
)


def test_committed_anomalies_pair_regenerates_exactly_and_shows_raw_clean_divergence(
    tmp_path: Path, tmp_out: Path
) -> None:
    """The committed spike pair regenerates exactly and diverges raw vs clean on one day."""
    scripts_dir = str(Path(__file__).resolve().parents[1] / "scripts")
    assert scripts_dir in sys.path, "conftest shim must expose scripts/ for stage imports"

    # tmp_out is the chart stage's per-test output directory: under tmp_path, never
    # the repository's out/, and empty before this test puts anything in it.
    assert tmp_out.is_dir(), "tmp_out must be an existing directory"
    assert tmp_out.parent == tmp_path, "tmp_out must live under tmp_path"
    assert tmp_out != FIXTURES_DIR, "tmp_out must not point into the committed fixtures"

    before = {name: FIXTURES_DIR.joinpath(name).read_bytes() for name in COMMITTED_FIXTURES}
    spec = json.loads(FIXTURES_DIR.joinpath("spec.example.json").read_text(encoding="utf-8"))
    committed = json.loads(
        FIXTURES_DIR.joinpath("metrics.anomalies.example.json").read_text(encoding="utf-8")
    )
    golden = json.loads(FIXTURES_DIR.joinpath("metrics.example.json").read_text(encoding="utf-8"))

    # Work on a copy: the committed CSV is an input, never a scratch file.
    working = tmp_out / "series.csv"
    working.write_bytes(before["series.anomalies.example.csv"])

    grouped = analyze_trends.load_series_csv(working, spec)
    regenerated = analyze_trends.build_metrics(
        spec, "tests/fixtures/spec.example.json", grouped
    )

    assert regenerated["series"] == committed["series"]
    assert set(committed) == {"spec_name", "as_of", "generated_from", "series"}
    # Key-set comparison, never a count literal: metrics.v1 may still grow fields.
    assert set(committed["series"][0]) == set(golden["series"][0])

    pl = next(s for s in committed["series"] if s["series_id"] == "pl-post-przerywany")
    assert [a["date"] for a in pl["anomalies"]] == ["2026-03-15"]
    assert pl["anomalies"][0]["value"] == 25380
    assert pl["anomalies"][0]["median"] == 2539
    # D-03 thesis made executable: one 10x day moves raw growth, not clean growth.
    assert pl["growth"]["y1"]["pct"] == 19.6
    assert pl["growth"]["y1"]["clean"]["pct"] == 16.7

    cs_committed = next(
        s for s in committed["series"] if s["series_id"] == "cs-pust-prerusovany"
    )
    cs_golden = next(s for s in golden["series"] if s["series_id"] == "cs-pust-prerusovany")
    assert cs_committed == cs_golden

    for name in COMMITTED_FIXTURES:
        assert FIXTURES_DIR.joinpath(name).read_bytes() == before[name], (
            f"the test run mutated the committed fixture {name}"
        )


def _write_spec(tmp_path: Path, series: list[dict[str, Any]]) -> Path:
    """Write a valid one-spec-per-call spec.json carrying exactly `series`."""
    golden = json.loads(FIXTURES_DIR.joinpath("spec.example.json").read_text(encoding="utf-8"))
    spec = {
        "name": "one-series-tracer",
        "request": "Порівняй зростання інтересу до інтервального голодування.",
        "language": golden["language"],
        "window": golden["window"],
        "series": series,
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    return path


def _spec_series_item(series_id: str) -> dict[str, Any]:
    """Return the committed spec's own series item, copied verbatim."""
    golden = json.loads(FIXTURES_DIR.joinpath("spec.example.json").read_text(encoding="utf-8"))
    return next(item for item in golden["series"] if item["id"] == series_id)


def _copy_fixtures(out_dir: Path, series_ids: tuple[str, ...]) -> None:
    """Materialize a working series.csv + metrics.json pair inside out_dir.

    The committed fixtures are inputs, never scratch files, so every chart test
    writes filtered *copies*. Filtering is required rather than cosmetic: the
    analyzer reader rejects a series_id absent from the spec, and the plan
    iterates metrics.series - so a one-series spec needs one-series inputs.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = FIXTURES_DIR.joinpath("series.example.csv").read_bytes()
    lines = raw.splitlines(keepends=True)
    header, rows = lines[0], lines[1:]
    kept = [
        line
        for line in rows
        if line.decode("utf-8").split(",")[2:3] and line.decode("utf-8").split(",")[2] in series_ids
    ]
    assert len(kept) > 0, "fixture filter kept no rows"
    out_dir.joinpath("series.csv").write_bytes(header + b"".join(kept))

    metrics = json.loads(FIXTURES_DIR.joinpath("metrics.example.json").read_text(encoding="utf-8"))
    metrics["series"] = [
        node for node in metrics["series"] if node["series_id"] in series_ids
    ]
    assert metrics["series"], "fixture filter kept no metrics series"
    out_dir.joinpath("metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _run_charts(spec_path: Path, out_dir: Path) -> int:
    return make_charts.main(["--spec", str(spec_path), "--out", str(out_dir)])


def _csv_views_by_series(path: Path) -> dict[str, list[int]]:
    """Read the working CSV independently of the analyzer, as a cross-check."""
    grouped: dict[str, list[tuple[str, int]]] = {}
    with path.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            grouped.setdefault(row["series_id"], []).append((row["date"], int(row["views"])))
    return {
        series_id: [views for _date, views in sorted(rows)]
        for series_id, rows in grouped.items()
    }


def test_tracer_one_series_renders_png_and_publishes_charts_json(tmp_path: Path, tmp_out: Path) -> None:
    """One validated spec renders exactly one real Agg PNG and one manifest."""
    spec_path = _write_spec(tmp_path, [_spec_series_item(TRACER_SERIES_ID)])
    _copy_fixtures(tmp_out, (TRACER_SERIES_ID,))

    # Case 1: a full CLI run, no monkeypatching, exit 0.
    assert _run_charts(spec_path, tmp_out) == 0

    # Case 2: one real PNG, 8 magic bytes, non-trivial size.
    pngs = sorted(tmp_out.glob("*.png"))
    assert len(pngs) == 1, f"expected exactly one rendered PNG, got {pngs}"
    blob = pngs[0].read_bytes()
    assert blob[:8] == PNG_MAGIC, "rendered chart must be a real PNG"
    assert len(blob) > 1024, "rendered chart must not be empty or truncated"

    manifest = json.loads(tmp_out.joinpath("charts.json").read_text(encoding="utf-8"))
    assert manifest["contract_version"] == "charts.v1"
    assert manifest["spec_name"] == "intermittent_fasting_pl_cs"
    assert manifest["language"] == "uk"
    assert len(manifest["charts"]) == 1
    assert manifest["charts"][0]["filename"] == pngs[0].name
    assert manifest["charts"][0]["kind"] == "timeseries"
    assert manifest["charts"][0]["series_id"] == TRACER_SERIES_ID
    assert manifest["charts"][0]["anomalies_drawn"] == 0
    assert manifest["charts"][0]["gaps"] == []

    # Cases 3-6 assert on the plan, not on pixels: the plan is the only place a
    # chart number exists (RESEARCH Pattern 1).
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    metrics, digest = make_charts.load_metrics(tmp_out / "metrics.json")
    grouped = analyze_trends.load_series_csv(tmp_out / "series.csv", spec)
    document = make_charts.build_chart_plan(
        spec, metrics, digest, grouped, str(tmp_out / "metrics.json")
    )
    entry = document.charts[0]

    # Case 3: the plotted raw values are the CSV views, in date order, exactly.
    expected_views = _csv_views_by_series(tmp_out / "series.csv")[TRACER_SERIES_ID]
    assert list(entry.raw_values) == expected_views
    assert entry.points[0].date.isoformat() == "2024-09-23"

    # Case 4: the median is the centered 7-day window median of those values,
    # truncated at the series edges and never labelled as anything.
    assert list(entry.median_values) == [
        float(median(entry.raw_values[max(0, index - 3) : index + 4]))
        for index in range(len(entry.raw_values))
    ]

    # Case 5: D-14's zero anchor is the literal 0.0.
    assert entry.y_limits[0] == 0.0
    assert entry.y_limits[1] > 0.0

    # Case 6: the manifest digest is sha256 of the exact metrics.json bytes read.
    assert manifest["metrics_sha256"] == digest
    assert manifest["metrics_sha256"] == make_charts.load_metrics(
        tmp_out / "metrics.json"
    )[1]
