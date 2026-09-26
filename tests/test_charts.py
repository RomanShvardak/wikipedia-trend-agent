"""Offline tests for the Wikipedia trend chart stage.

Zero-network guarantee: this module never opens a socket — the chart stage reads
only committed fixtures plus the analyzer's own pure helpers, and every test
asserts the committed fixture bytes are unchanged after it runs.
"""
from __future__ import annotations

import ast
import csv
import hashlib
import json
import sys
from pathlib import Path
from statistics import median
from typing import Any

import pytest

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


# --- Task 2: the guard-rail surface for the layer Task 1 built -------------


def _render_two_series(out_dir: Path) -> tuple[Path, dict[str, Any]]:
    """Render the committed two-series pair and return (spec, published manifest)."""
    spec_path = FIXTURES_DIR / "spec.example.json"
    _copy_fixtures(out_dir, ALL_SERIES_IDS)
    assert _run_charts(spec_path, out_dir) == 0
    manifest = json.loads(out_dir.joinpath("charts.json").read_text(encoding="utf-8"))
    return spec_path, manifest


def _plan(out_dir: Path, spec_path: Path) -> make_charts.ChartDocument:
    """Rebuild the chart plan from the same inputs the CLI read."""
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    metrics, digest = make_charts.load_metrics(out_dir / "metrics.json")
    grouped = analyze_trends.load_series_csv(out_dir / "series.csv", spec)
    return make_charts.build_chart_plan(
        spec, metrics, digest, grouped, str(out_dir / "metrics.json")
    )


def _module_source() -> str:
    """Read the chart module's own source, path resolved once here."""
    return Path(str(make_charts.__file__)).read_text(encoding="utf-8")


def test_backend_is_agg() -> None:
    """matplotlib.use("Agg") precedes the first pyplot import, and Agg is live."""
    source = _module_source()
    tree = ast.parse(source)
    use_lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "use"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "matplotlib"
    ]
    pyplot_lines = [
        node.lineno
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Import)
            and any(alias.name == "matplotlib.pyplot" for alias in node.names)
        )
        or (
            isinstance(node, ast.ImportFrom) and node.module == "matplotlib.pyplot"
        )
    ]
    assert use_lines, 'the module must call matplotlib.use("Agg") explicitly'
    assert pyplot_lines, "the render path must import matplotlib.pyplot"
    assert min(use_lines) < min(pyplot_lines), (
        'matplotlib.use("Agg") must precede the first import matplotlib.pyplot'
    )

    import matplotlib
    import matplotlib.pyplot  # noqa: F401 (imported only to bind the backend)

    assert matplotlib.get_backend().lower() == "agg"


def test_charts_module_ast_forbids_dynamic_execution_and_heavy_dependencies() -> None:
    """The chart module imports no heavy dependency, no `os`, and never evaluates code."""
    tree = ast.parse(_module_source())
    forbidden_calls = {"eval", "exec", "compile", "__import__", "system", "popen", "run"}
    forbidden_imports = {
        "subprocess",
        "pickle",
        "multiprocessing",
        "runpy",
        "os",
        "numpy",
        "pandas",
        "seaborn",
        "plotly",
        "altair",
        "statsmodels",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in forbidden_calls, (
                f"make_charts must not call {node.func.id}()"
            )
        if isinstance(node, ast.Import):
            assert not {alias.name.split(".")[0] for alias in node.names} & forbidden_imports
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in forbidden_imports


def test_png_magic_bytes_for_every_chart(tmp_out: Path) -> None:
    """Every manifest filename is a real, non-trivial PNG on disk."""
    _spec, manifest = _render_two_series(tmp_out)
    assert manifest["charts"], "the two-series run must publish at least one chart"
    for entry in manifest["charts"]:
        blob = (tmp_out / entry["filename"]).read_bytes()
        assert blob[:8] == PNG_MAGIC, f"{entry['filename']} must begin with PNG magic bytes"
        assert len(blob) > 1024, f"{entry['filename']} must not be empty or truncated"


def test_timeseries_has_raw_and_median_series(tmp_out: Path) -> None:
    """The plan's raw values are the CSV views; its median is the centered 7-day median."""
    spec_path, _manifest = _render_two_series(tmp_out)
    views_by_series = _csv_views_by_series(tmp_out / "series.csv")

    for entry in _plan(tmp_out, spec_path).charts:
        expected = views_by_series[entry.series_id]
        assert list(entry.raw_values) == expected, (
            "the plotted raw line must be the series.csv views in date order, exactly"
        )
        assert list(entry.median_values) == [
            float(median(expected[max(0, index - 3) : index + 4]))
            for index in range(len(expected))
        ]
        assert len(entry.median_values) == len(entry.raw_values), (
            "the two lines must be drawable against the same x positions"
        )
        assert len(entry.points) == len(expected)


def test_timeseries_y_limits_anchored_at_zero(tmp_out: Path) -> None:
    """D-14: the lower bound is exactly 0.0, not merely bounded below by zero."""
    spec_path, _manifest = _render_two_series(tmp_out)
    for entry in _plan(tmp_out, spec_path).charts:
        assert entry.y_limits[0] == 0.0
        assert entry.y_limits[1] > 0.0


def test_charts_json_manifest_matches_rendered_files(tmp_out: Path) -> None:
    """No orphan PNG and no manifest entry without a file; filenames are bare names."""
    _spec, manifest = _render_two_series(tmp_out)
    on_disk = {path.name for path in tmp_out.glob("*.png")}
    recorded = {entry["filename"] for entry in manifest["charts"]}
    assert on_disk == recorded, "manifest filenames and rendered PNGs must be the same set"
    for name in recorded:
        assert "/" not in name and "\\" not in name, f"{name} must be a bare relative name"
        assert ".." not in Path(name).parts, f"{name} must not contain a .. segment"


def test_manifest_records_metrics_digest(tmp_out: Path) -> None:
    """metrics_sha256 tracks the metrics.json content, so staleness needs no timestamps."""
    spec_path = FIXTURES_DIR / "spec.example.json"
    _copy_fixtures(tmp_out, ALL_SERIES_IDS)
    assert _run_charts(spec_path, tmp_out) == 0
    first = json.loads(tmp_out.joinpath("charts.json").read_text(encoding="utf-8"))[
        "metrics_sha256"
    ]
    assert first == hashlib.sha256((tmp_out / "metrics.json").read_bytes()).hexdigest()

    # A single added space is semantically identical JSON but different bytes:
    # if the digest were a constant, this re-run would reproduce it. The working
    # copy is edited in place - re-copying the fixture would undo the change.
    (tmp_out / "metrics.json").write_bytes((tmp_out / "metrics.json").read_bytes() + b" ")
    assert _run_charts(spec_path, tmp_out) == 0
    second = json.loads(tmp_out.joinpath("charts.json").read_text(encoding="utf-8"))[
        "metrics_sha256"
    ]
    assert second == hashlib.sha256((tmp_out / "metrics.json").read_bytes()).hexdigest()
    assert second != first, "the digest must track content, not a constant"


def test_manifest_never_emits_null_values(tmp_out: Path) -> None:
    """RPT-02's no-leak rule at the chart stage: optional keys are omitted, not nulled."""
    _spec, manifest = _render_two_series(tmp_out)
    for key, value in manifest.items():
        assert value is not None, f"charts.json must never carry a null at {key!r}"
        if isinstance(value, str):
            assert value not in {"None", "nan"}, f"charts.json leaked {value!r}"
        elif isinstance(value, list):
            for item in value:
                assert isinstance(item, dict), f"charts.json {key}[] must hold objects"
                for inner_key, inner in item.items():
                    assert inner is not None, (
                        f"charts.json must never carry a null at {key}[].{inner_key}"
                    )
                    if isinstance(inner, str):
                        assert inner not in {"None", "nan"}, f"charts.json leaked {inner!r}"
                    elif isinstance(inner, list):
                        assert all(
                            member is not None and member != "nan" for member in inner
                        ), f"charts.json leaked inside {key}[].{inner_key}"


def test_missing_metrics_exits_nonzero_without_writing(
    tmp_out: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing metrics.json fails closed: exit 1, model-readable stderr, no artifact."""
    spec_path = FIXTURES_DIR / "spec.example.json"
    _copy_fixtures(tmp_out, ALL_SERIES_IDS)
    sentinel = tmp_out / "charts.json"
    sentinel.write_bytes(b"sentinel")
    stub = tmp_out / "chart_pl-post-przerywany_timeseries.png"
    stub.write_bytes(b"stub")
    (tmp_out / "metrics.json").unlink()

    assert _run_charts(spec_path, tmp_out) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("charts failed:"), captured.err
    assert sentinel.read_bytes() == b"sentinel", "a failed run must not publish a manifest"
    assert stub.read_bytes() == b"stub", "a failed run must not replace a chart"
    assert sorted(path.name for path in tmp_out.glob("*.png")) == [stub.name], (
        "a failed run must not write any new chart"
    )
    assert captured.out == "", "a failed run must not print a success line"
