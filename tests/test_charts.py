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
from datetime import date, timedelta
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


def _write_csv_filtered(out_dir: Path, series_ids: tuple[str, ...]) -> None:
    """Write only `series_ids`' rows of the committed CSV into out_dir/series.csv.

    Filtering is byte-level line selection, so the CRLF row endings and the exact
    `views` cells of the committed fixture survive; the committed files are
    inputs, never scratch files. The analyzer reader rejects a CSV series_id
    absent from the spec, which is why the CSV can be narrowed independently of
    metrics.json when a test needs the two documents to disagree.
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


def _write_metrics_filtered(out_dir: Path, series_ids: tuple[str, ...]) -> None:
    """Write a metrics.json whose series array is filtered to `series_ids`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics = json.loads(FIXTURES_DIR.joinpath("metrics.example.json").read_text(encoding="utf-8"))
    metrics["series"] = [
        node for node in metrics["series"] if node["series_id"] in series_ids
    ]
    assert metrics["series"], "fixture filter kept no metrics series"
    out_dir.joinpath("metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _copy_fixtures(out_dir: Path, series_ids: tuple[str, ...]) -> None:
    """Materialize a working series.csv + metrics.json pair inside out_dir.

    The committed fixtures are inputs, never scratch files, so every chart test
    writes filtered *copies*. Filtering is required rather than cosmetic: the
    analyzer reader rejects a series_id absent from the spec, and the plan
    iterates metrics.series - so a one-series spec needs one-series inputs.
    """
    _write_csv_filtered(out_dir, series_ids)
    _write_metrics_filtered(out_dir, series_ids)


def _metrics_series_order() -> tuple[str, ...]:
    """The committed golden metrics series order - the SSoT for chart ordering."""
    metrics = json.loads(FIXTURES_DIR.joinpath("metrics.example.json").read_text(encoding="utf-8"))
    return tuple(node["series_id"] for node in metrics["series"])


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

    # Case 2: one real PNG per rendered chart, 8 magic bytes, non-trivial size.
    # N=1 publishes the two per-series kinds plus the comparison view (D-19: 2N+1),
    # which is 3 - half of the 5 the two-series fixture reaches.
    pngs = sorted(tmp_out.glob("*.png"))
    assert len(pngs) == 3, f"expected two per-series charts and the overlay, got {pngs}"
    for png in pngs:
        blob = png.read_bytes()
        assert blob[:8] == PNG_MAGIC, "rendered chart must be a real PNG"
        assert len(blob) > 1024, "rendered chart must not be empty or truncated"

    manifest = json.loads(tmp_out.joinpath("charts.json").read_text(encoding="utf-8"))
    assert manifest["contract_version"] == "charts.v1"
    assert manifest["spec_name"] == "intermittent_fasting_pl_cs"
    assert manifest["language"] == "uk"
    assert len(manifest["charts"]) == 3
    assert sorted(entry["filename"] for entry in manifest["charts"]) == [png.name for png in pngs]
    timeseries_entry = next(
        entry for entry in manifest["charts"] if entry["kind"] == make_charts.TIMESERIES_KIND
    )
    assert timeseries_entry["filename"] == f"chart_{TRACER_SERIES_ID}_timeseries.png"
    assert timeseries_entry["series_id"] == TRACER_SERIES_ID
    assert timeseries_entry["anomalies_drawn"] == 0
    assert timeseries_entry["gaps"] == []

    # Cases 3-6 assert on the plan, not on pixels: the plan is the only place a
    # chart number exists (RESEARCH Pattern 1).
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    metrics, digest = make_charts.load_metrics(tmp_out / "metrics.json")
    grouped = analyze_trends.load_series_csv(tmp_out / "series.csv", spec)
    document = make_charts.build_chart_plan(
        spec, metrics, digest, grouped, str(tmp_out / "metrics.json")
    )
    entry = next(c for c in document.charts if c.kind == make_charts.TIMESERIES_KIND)

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

    # Filtered on kind, not on index: the comparison view carries no series of its
    # own and is asserted separately.
    for entry in _plan(tmp_out, spec_path).charts:
        if entry.kind != make_charts.TIMESERIES_KIND:
            continue
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
    """D-14: the lower bound is exactly 0.0, not merely bounded below by zero.

    Filtered to the two *timeseries* axes by kind. Zero anchoring is a
    timeseries/overlay rule only (D-14 constrains `views`, which is
    non-negative by contract); the growth chart's value axis is a percentage
    axis that must be free to go below zero, or a decline is erased (RESEARCH
    Pitfall 1). Plan 05-04 adds that third kind, so this test names the kinds
    it is about rather than every entry in the document.
    """
    spec_path, _manifest = _render_two_series(tmp_out)
    timeseries_axes = {
        make_charts.TIMESERIES_KIND,
        make_charts.OVERLAY_KIND,
    }
    seen = 0
    for entry in _plan(tmp_out, spec_path).charts:
        if entry.kind not in timeseries_axes:
            continue
        assert entry.y_limits[0] == 0.0
        assert entry.y_limits[1] > 0.0
        seen += 1
    assert seen, "the timeseries/overlay axes must still be asserted"


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


# --- Plan 05-03 Task 1: N-series scaling, deterministic names, unsafe-id refusal

# One series_id per character class the filename guard must reject. `../../etc/passwd`
# is a path escape; the rest prove the charset is ASCII-only, not merely
# separator-free (T-5-01).
UNSAFE_SERIES_IDS = (
    "../../etc/passwd",
    "with space",
    "café",
    "a/b",
    "a:b",
)


def test_inventory_is_two_png_per_series_plus_overlay(tmp_out: Path) -> None:
    """D-19's 2N+1, closed and pinned: two per-series kinds plus one overlay."""
    spec_path, manifest = _render_two_series(tmp_out)
    document = _plan(tmp_out, spec_path)
    order = _metrics_series_order()
    per_series = [entry for entry in document.charts if entry.series_id is not None]
    overlay = [entry for entry in document.charts if entry.kind == make_charts.OVERLAY_KIND]
    timeseries = [entry for entry in per_series if entry.kind == make_charts.TIMESERIES_KIND]
    growth = [entry for entry in per_series if entry.kind == make_charts.GROWTH_KIND]

    # One entry of each per-series kind per series, in metrics.json series order.
    # CONTRACTS.md §3 freezes that order as spec.series[] order, so nothing may
    # sort it away.
    assert [entry.series_id for entry in timeseries] == list(order)
    assert [entry.series_id for entry in growth] == list(order)
    assert [entry.spec_index for entry in timeseries] == list(range(len(order)))
    assert [entry.spec_index for entry in growth] == list(range(len(order)))
    assert [entry.spec_index for entry in per_series] == sorted(
        entry.spec_index for entry in per_series
    ), "charts[] must follow spec order, never a sort by label or language"

    # A chart is identified by the pair (series_id, kind): no duplicate pair, and
    # the per-series side of the inventory is exactly N x 2.
    pairs = [(entry.series_id, entry.kind) for entry in per_series]
    assert len(pairs) == len(set(pairs)), "one entry per (series_id, kind) pair"
    assert {entry.kind for entry in per_series} == {
        make_charts.TIMESERIES_KIND,
        make_charts.GROWTH_KIND,
    }, "D-19 names exactly two per-series kinds"
    assert len(per_series) == len(order) * 2
    assert len(overlay) == 1, "the comparison view must never be conditional on N > 1"

    # D-19's literal 2N+1, and the kind sequence it produces. Per-series kinds
    # are adjacent siblings in spec order; the overlay is last.
    assert len(document.charts) == 2 * len(order) + 1
    assert len(document.charts) == 5
    assert [entry.kind for entry in document.charts] == [
        make_charts.TIMESERIES_KIND,
        make_charts.GROWTH_KIND,
        make_charts.TIMESERIES_KIND,
        make_charts.GROWTH_KIND,
        make_charts.OVERLAY_KIND,
    ]
    assert document.charts[-1].kind == make_charts.OVERLAY_KIND, "the overlay is last"

    # Count from the manifest, never from a directory glob: an orphan file must
    # not be able to stand in for a designed chart, and a dangling entry must
    # not be able to hide.
    assert len(manifest["charts"]) == len(document.charts) == 5
    assert {path.name for path in tmp_out.glob("*.png")} == {
        entry["filename"] for entry in manifest["charts"]
    }
    assert len(list(tmp_out.glob("*.png"))) == 5, "exactly five PNGs on disk"
    assert make_charts.OVERLAY_FILENAME in {path.name for path in tmp_out.glob("*.png")}
    assert [entry["spec_index"] for entry in manifest["charts"] if "series_id" in entry] == [
        entry.spec_index for entry in per_series
    ], "the manifest must publish the same spec order the plan declares"


@pytest.mark.parametrize(
    ("series_ids", "expected_total"),
    [(("pl-post-przerywany",), 3), (("cs-pust-prerusovany", "pl-post-przerywany"), 5)],
    ids=["N=1", "N=2"],
)
def test_inventory_total_is_two_per_series_plus_one_overlay(
    tmp_path: Path,
    series_ids: tuple[str, ...],
    expected_total: int,
) -> None:
    """The 2N+1 count holds for any N, checked from the plan and from the disk."""
    out_dir = tmp_path / "out"
    if len(series_ids) == 1:
        spec_path = _write_spec(tmp_path, [_spec_series_item(series_ids[0])])
    else:
        spec_path = FIXTURES_DIR / "spec.example.json"
    _copy_fixtures(out_dir, series_ids)
    assert _run_charts(spec_path, out_dir) == 0

    document = _plan(out_dir, spec_path)
    assert len(document.charts) == 2 * len(series_ids) + 1
    assert len(document.charts) == expected_total
    assert [c.kind for c in document.charts].count(make_charts.OVERLAY_KIND) == 1
    manifest = json.loads(out_dir.joinpath("charts.json").read_text(encoding="utf-8"))
    assert len(manifest["charts"]) == expected_total
    assert len(list(out_dir.glob("*.png"))) == expected_total


def test_filenames_are_deterministic_and_derived_from_series_id(tmp_path: Path) -> None:
    """The same inputs produce the same names in the same order, from series_id alone."""
    spec_path = FIXTURES_DIR / "spec.example.json"
    first_dir, second_dir = tmp_path / "run-a", tmp_path / "run-b"
    _copy_fixtures(first_dir, ALL_SERIES_IDS)
    _copy_fixtures(second_dir, ALL_SERIES_IDS)
    assert _run_charts(spec_path, first_dir) == 0
    assert _run_charts(spec_path, second_dir) == 0

    first = json.loads(first_dir.joinpath("charts.json").read_text(encoding="utf-8"))["charts"]
    second = json.loads(second_dir.joinpath("charts.json").read_text(encoding="utf-8"))["charts"]
    assert [entry["filename"] for entry in first] == [entry["filename"] for entry in second]

    for entry in first:
        if entry["kind"] == make_charts.OVERLAY_KIND:
            continue
        assert entry["filename"] == f"chart_{entry['series_id']}_{entry['kind']}.png"
    # T-5-09: because an unsafe id is refused rather than sanitized, two distinct
    # ids can never map to one name - and a future kind constant must not collide
    # with the fixed overlay name either.
    names = [entry["filename"] for entry in first]
    assert len(names) == len(set(names)), "filenames must be unique within a run"
    assert make_charts.OVERLAY_FILENAME not in {
        entry["filename"] for entry in first if entry["kind"] != make_charts.OVERLAY_KIND
    }


@pytest.mark.parametrize("unsafe_id", UNSAFE_SERIES_IDS)
def test_unsafe_series_id_is_refused_without_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], unsafe_id: str
) -> None:
    """T-5-01: an unsafe series_id is refused, never sanitized, and writes nothing."""
    out_dir = tmp_path / "out"
    spec_path = _write_unsafe_series_inputs(tmp_path, out_dir, unsafe_id)

    assert _run_charts(spec_path, out_dir) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("charts failed:"), captured.err
    assert "not filename-safe" in captured.err
    assert unsafe_id in captured.err, "the refusal must name the offending id"
    assert list(out_dir.glob("*.png")) == [], "a refused series must write no PNG"
    assert not out_dir.joinpath("charts.json").exists(), "a refused series must publish no manifest"
    assert sorted(path.name for path in out_dir.iterdir()) == ["metrics.json", "series.csv"]
    # Nothing outside --out either: the id is refused before any path is built.
    assert sorted(path.name for path in tmp_path.iterdir()) == ["out", "spec.json"]


def _write_unsafe_series_inputs(tmp_path: Path, out_dir: Path, unsafe_id: str) -> Path:
    """Materialize a one-series spec whose id is `unsafe_id`, through all three documents.

    The unsafe id has to reach build_chart_plan for the refusal to be the thing
    under test, so the spec item, the CSV rows and the metrics series all carry
    it: analyze_trends.load_series_csv rejects a CSV series_id absent from the
    spec, and the chart plan joins metrics.series against spec.series.
    """
    item = dict(_spec_series_item(TRACER_SERIES_ID))
    item["id"] = unsafe_id
    spec_path = _write_spec(tmp_path, [item])

    out_dir.mkdir(parents=True, exist_ok=True)
    raw = FIXTURES_DIR.joinpath("series.example.csv").read_bytes()
    lines = raw.splitlines(keepends=True)
    header, rows = lines[0], lines[1:]
    kept: list[bytes] = []
    for line in rows:
        fields = line.split(b",")
        if fields[2].decode("utf-8") == TRACER_SERIES_ID:
            fields[2] = unsafe_id.encode("utf-8")
            kept.append(b",".join(fields))
    assert kept, "no tracer rows found to rename"
    out_dir.joinpath("series.csv").write_bytes(header + b"".join(kept))

    metrics = json.loads(FIXTURES_DIR.joinpath("metrics.example.json").read_text(encoding="utf-8"))
    node = dict(next(n for n in metrics["series"] if n["series_id"] == TRACER_SERIES_ID))
    node["series_id"] = unsafe_id
    metrics["series"] = [node]
    out_dir.joinpath("metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return spec_path


def test_series_with_no_rows_fails_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Absent data is never turned into a chart: the stage fails closed instead."""
    out_dir = tmp_path / "out"
    # Both series are declared, so the analyzer reader is the one that must refuse
    # the CSV's missing series before the plan can invent a chart for it.
    spec_path = _write_spec(tmp_path, [_spec_series_item(sid) for sid in ALL_SERIES_IDS])
    _write_csv_filtered(out_dir, (TRACER_SERIES_ID,))
    _write_metrics_filtered(out_dir, ALL_SERIES_IDS)

    assert _run_charts(spec_path, out_dir) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("charts failed:"), captured.err
    missing = next(sid for sid in ALL_SERIES_IDS if sid != TRACER_SERIES_ID)
    assert missing in captured.err, "the failure must name the series that has no rows"
    assert "no observations for" in captured.err
    assert list(out_dir.glob("*.png")) == []
    assert not out_dir.joinpath("charts.json").exists()

    # The plan's own guard is the second line of defence, and it is unreachable
    # through main() precisely because the reader above already refuses. Asserted
    # on the plan directly so the branch cannot rot: an empty observation list
    # must raise ChartError naming the series, never produce an empty chart.
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    metrics, digest = make_charts.load_metrics(out_dir / "metrics.json")
    grouped = analyze_trends.load_series_csv(FIXTURES_DIR / "series.example.csv", spec)
    del grouped[missing]
    with pytest.raises(make_charts.ChartError) as raised:
        make_charts.build_chart_plan(spec, metrics, digest, grouped, str(out_dir / "metrics.json"))
    assert str(raised.value) == f"no observations for series: {missing}"


def test_metrics_series_absent_from_spec_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T-5-02: the plan joins metrics against the spec instead of trusting either."""
    out_dir = tmp_path / "out"
    # A spec declaring one series, a CSV carrying that series, and metrics.json
    # listing two: the chart stage must refuse the mismatch before it can label
    # a chart with the wrong language or article.
    spec_path = _write_spec(tmp_path, [_spec_series_item(TRACER_SERIES_ID)])
    _write_csv_filtered(out_dir, (TRACER_SERIES_ID,))
    _write_metrics_filtered(out_dir, ALL_SERIES_IDS)

    assert _run_charts(spec_path, out_dir) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("charts failed:"), captured.err
    assert "is not in spec" in captured.err
    missing = next(sid for sid in ALL_SERIES_IDS if sid != TRACER_SERIES_ID)
    assert missing in captured.err
    assert list(out_dir.glob("*.png")) == []
    assert not out_dir.joinpath("charts.json").exists()


# --- Plan 05-04 Task 1: growth bars - three windows, clean-only, n/a (D-02..D-05)


def _growth_entry_of(
    document: make_charts.ChartDocument, series_id: str = TRACER_SERIES_ID
) -> make_charts.ChartEntry:
    """The one growth entry a series owns, by kind and id rather than by index."""
    growth = [
        entry
        for entry in document.charts
        if entry.kind == make_charts.GROWTH_KIND and entry.series_id == series_id
    ]
    assert len(growth) == 1, f"exactly one growth chart for {series_id}"
    return growth[0]


def _published_growth_of(manifest: dict[str, Any], series_id: str) -> dict[str, Any]:
    """The manifest's growth entry for one series, selected by kind and id."""
    published = [
        item
        for item in manifest["charts"]
        if item["kind"] == make_charts.GROWTH_KIND and item.get("series_id") == series_id
    ]
    assert len(published) == 1, f"exactly one published growth chart for {series_id}"
    return published[0]


def _metrics_by_series(out_dir: Path) -> dict[str, dict[str, Any]]:
    """The working metrics.json keyed by series_id, read as plain JSON."""
    document = json.loads(out_dir.joinpath("metrics.json").read_text(encoding="utf-8"))
    return {node["series_id"]: node for node in document["series"]}


def test_growth_bars_cover_three_windows_with_labels(tmp_out: Path) -> None:
    """D-02: all three windows get a bar, in contract order, each with a label."""
    spec_path, manifest = _render_two_series(tmp_out)
    document = _plan(tmp_out, spec_path)
    entry = _growth_entry_of(document)
    assert entry.kind == make_charts.GROWTH_KIND

    assert [bar.window for bar in entry.bars] == ["m3", "y1", "y2"]
    published = _published_growth_of(manifest, TRACER_SERIES_ID)
    assert [bar["window"] for bar in published["bars"]] == ["m3", "y1", "y2"]
    # D-05: every bar names its window, so the reader never maps tick position
    # to window by counting rows.
    for bar in published["bars"]:
        assert bar["label"] == make_charts.WINDOW_LABELS[bar["window"]]
        assert bar["label"]
    assert [bar["label"] for bar in published["bars"]] == ["3M", "1Y", "2Y"]


def test_growth_bars_equal_metrics_clean_pct(tmp_out: Path) -> None:
    """CHRT-02's single-lineage assertion: clean.pct verbatim, plain `==`, no tolerance."""
    spec_path, _manifest = _render_two_series(tmp_out)
    document = _plan(tmp_out, spec_path)
    metrics = _metrics_by_series(tmp_out)

    growth_entries = [c for c in document.charts if c.kind == make_charts.GROWTH_KIND]
    assert len(growth_entries) == len(metrics)
    for entry in growth_entries:
        series_id = entry.series_id
        assert series_id is not None
        node = metrics[series_id]
        for bar in entry.bars:
            clean = node["growth"][bar.window]["clean"]
            # Plain equality: a rounded or float-drifted copy fails here, which is
            # the whole point - the bar IS the contract number, not a rendering of it.
            assert bar.pct == clean["pct"], (
                f"{series_id}/{bar.window}: the bar must be growth.{bar.window}.clean.pct "
                f"verbatim, never a re-derived or rounded value"
            )
            assert bar.abs == clean["abs"]
            # T-5-13: the volume base is metrics.json's own field, never a mean
            # recomputed from series.csv.
            assert bar.base_avg_daily_views == node["avg_daily_views"]


def test_growth_bars_never_carry_the_raw_value(tmp_path: Path, tmp_out: Path) -> None:
    """D-03: only `clean.pct` is plotted; the raw spike number never reaches a bar."""
    out_dir = tmp_path / "anomalies"
    _write_anomaly_pair(out_dir)
    assert _run_charts(FIXTURES_DIR / "spec.example.json", out_dir) == 0

    document = _plan(out_dir, FIXTURES_DIR / "spec.example.json")
    entry = _growth_entry_of(document)
    manifest = json.loads(out_dir.joinpath("charts.json").read_text(encoding="utf-8"))
    published = _published_growth_of(manifest, TRACER_SERIES_ID)

    pl_bars = {bar["window"]: bar for bar in published["bars"]}
    # The fixture's 10x day moved the raw 1Y growth to +19.6 and the clean to
    # +16.7. The bar is the second number, and the first is nowhere on the chart.
    assert pl_bars["y1"]["pct"] == 16.7
    assert pl_bars["y1"]["pct"] != 19.6
    serialized = json.dumps(published["bars"], ensure_ascii=False)
    assert "19.6" not in serialized, "the raw pct must never appear in a growth bar"
    assert 19.6 not in [bar.pct for bar in entry.bars]


def _write_anomaly_pair(out_dir: Path) -> None:
    """Materialize the committed spike pair (series + metrics) inside out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.joinpath("series.csv").write_bytes(
        FIXTURES_DIR.joinpath("series.anomalies.example.csv").read_bytes()
    )
    out_dir.joinpath("metrics.json").write_bytes(
        FIXTURES_DIR.joinpath("metrics.anomalies.example.json").read_bytes()
    )


def test_null_growth_is_labelled_na_with_reason(tmp_out: Path) -> None:
    """D-04: a null is an explicit n/a bar carrying the contract reason, not a gap."""
    spec_path, manifest = _render_two_series(tmp_out)
    entry = _growth_entry_of(_plan(tmp_out, spec_path))
    published = _published_growth_of(manifest, TRACER_SERIES_ID)
    bars = {bar["window"]: bar for bar in published["bars"]}

    null_bars = [bar for bar in entry.bars if bar.pct is None]
    assert len(null_bars) == 1, "the committed golden has exactly one null window"
    assert [bar.window for bar in null_bars] == ["y2"]
    assert null_bars[0].reason == (
        "insufficient observations in one or both equal-length windows"
    )
    assert null_bars[0].abs is None

    y2 = bars["y2"]
    # The no-null manifest rule (05-02): the keys are omitted, never nulled, and
    # the documented boolean says which state the bar is in.
    assert y2["bar_null"] is True
    assert "pct" not in y2
    assert "abs" not in y2
    assert y2["reason"] == "insufficient observations in one or both equal-length windows"
    for other_window in ("m3", "y1"):
        assert bars[other_window]["bar_null"] is False
        assert "reason" not in bars[other_window]
        assert bars[other_window]["pct"] is not None
    # No null *value* anywhere in the serialized bars payload. Checked by walking
    # values rather than by substring search, because the key name `bar_null`
    # itself contains the characters "null".
    for bar in published["bars"]:
        for key, value in bar.items():
            assert value is not None, f"charts.json must never carry a null at bars.{key}"
            if isinstance(value, str):
                assert value not in {"None", "nan"}, f"charts.json leaked {value!r}"
    assert "null" not in json.dumps(published["bars"], ensure_ascii=False).replace(
        "bar_null", ""
    )


def _write_golden_copy(out_dir: Path, series_ids: tuple[str, ...] = ALL_SERIES_IDS) -> None:
    """Copy the golden pair into out_dir, then let a test edit metrics.json in place."""
    _copy_fixtures(out_dir, series_ids)


def test_null_growth_without_reason_fails_closed(
    tmp_out: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A null with no reason is a contract violation, refused before anything is drawn."""
    _write_golden_copy(tmp_out)
    path = tmp_out / "metrics.json"
    metrics = json.loads(path.read_text(encoding="utf-8"))
    node = next(n for n in metrics["series"] if n["series_id"] == TRACER_SERIES_ID)
    del node["growth"]["y2"]["clean"]["reason"]
    path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    assert _run_charts(FIXTURES_DIR / "spec.example.json", tmp_out) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("charts failed:"), captured.err
    assert "null without a reason" in captured.err
    assert TRACER_SERIES_ID in captured.err
    assert list(tmp_out.glob("*.png")) == []
    assert not tmp_out.joinpath("charts.json").exists()


def test_missing_growth_window_fails_closed(
    tmp_out: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D-02: all three windows are required, because an absent 2Y is itself signal."""
    _write_golden_copy(tmp_out)
    path = tmp_out / "metrics.json"
    metrics = json.loads(path.read_text(encoding="utf-8"))
    node = next(n for n in metrics["series"] if n["series_id"] == TRACER_SERIES_ID)
    del node["growth"]["y2"]
    path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    assert _run_charts(FIXTURES_DIR / "spec.example.json", tmp_out) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("charts failed:"), captured.err
    assert "missing growth window" in captured.err
    assert "'y2'" in captured.err
    assert TRACER_SERIES_ID in captured.err
    assert list(tmp_out.glob("*.png")) == []


# --- Plan 05-04 Task 2: the negative-bar guarantee, 0 != null, and 2N+1


def _write_growth_pair_with_pct(
    out_dir: Path, series_id: str, window: str, pct: float, absolute: int
) -> None:
    """Copy the golden pair into out_dir, then set one window's clean pct/abs."""
    _copy_fixtures(out_dir, ALL_SERIES_IDS)
    path = out_dir / "metrics.json"
    metrics = json.loads(path.read_text(encoding="utf-8"))
    node = next(n for n in metrics["series"] if n["series_id"] == series_id)
    node["growth"][window]["clean"]["pct"] = pct
    node["growth"][window]["clean"]["abs"] = absolute
    path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")


def test_negative_growth_bar_stays_within_y_limits(tmp_out: Path) -> None:
    """RESEARCH Pitfall 1: a -22.0% decline must render downward, never be clipped.

    The measured failure was `set_ylim(0, None)` on `[3.5, 16.7, -22.0]`
    producing `ylim (0.0, 18.63)` - the decline entirely outside the visible
    axis, so a falling topic read as a small rising one. On this horizontal form
    the same truncation is `set_xlim(left=0)`, which the branch never calls.
    """
    _write_growth_pair_with_pct(tmp_out, TRACER_SERIES_ID, "y1", -22.0, -4860)
    assert _run_charts(FIXTURES_DIR / "spec.example.json", tmp_out) == 0

    document = _plan(tmp_out, FIXTURES_DIR / "spec.example.json")
    entry = _growth_entry_of(document, TRACER_SERIES_ID)

    # The value-axis floor is below the most negative bar, so nothing is clipped.
    assert entry.y_limits[0] < 0.0
    assert entry.y_limits[0] <= -22.0
    # And the ceiling still clears the largest positive bar on the same chart
    # (3M's +3.5% - the 1Y bar it replaced was the +16.7%).
    assert entry.y_limits[1] >= 3.5

    bars = {bar.window: bar for bar in entry.bars}
    # The bar's value is the negative pct exactly, drawn downward from zero.
    assert bars["y1"].pct == -22.0
    assert bars["y1"].abs == -4860
    # All three render paths are on one chart: positive, negative, and null.
    assert bars["m3"].pct == 3.5
    assert bars["y2"].pct is None

    manifest = json.loads(tmp_out.joinpath("charts.json").read_text(encoding="utf-8"))
    published = _published_growth_of(manifest, TRACER_SERIES_ID)
    assert published["y_limits"] == [entry.y_limits[0], entry.y_limits[1]]
    assert published["y_limits"][0] <= -22.0
    y1_bar = next(bar for bar in published["bars"] if bar["window"] == "y1")
    assert y1_bar["pct"] == -22.0
    assert y1_bar["bar_null"] is False

    blob = tmp_out.joinpath(published["filename"]).read_bytes()
    assert blob[:8] == PNG_MAGIC, "the growth chart must be a real PNG"
    assert len(blob) > 1024, "the growth chart must not be empty or truncated"


def test_growth_axes_never_receive_a_zero_floor() -> None:
    """The growth branch names no axis-limit call at all - the prohibition as a call.

    Scoped to the GROWTH_KIND branch of `render_chart` (and the draw helper it
    delegates to) rather than counted module-wide, so a legitimate
    `set_ylim` on the timeseries or overlay path cannot misfire this guard.
    """
    tree = ast.parse(_module_source())
    render_functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "render_chart"
    ]
    assert len(render_functions) == 1, "render_chart must be a single function"
    forbidden = {"set_xlim", "set_ylim", "set_xscale"}

    # The branch is located by the *identifier* `GROWTH_KIND`, so resolve that
    # identifier to its value in the module and require it to be the real kind
    # constant. Without this the guard would keep "passing" after a rename that
    # silently moved the growth branch somewhere the walk cannot see.
    constant_value = None
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "GROWTH_KIND"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
        ):
            constant_value = node.value.value
    assert constant_value == make_charts.GROWTH_KIND

    growth_branches: list[ast.AST] = []
    for node in ast.walk(render_functions[0]):
        if not isinstance(node, ast.If):
            continue
        comparison = node.test
        if not isinstance(comparison, ast.Compare) or len(comparison.comparators) != 1:
            continue
        comparator = comparison.comparators[0]
        if (
            isinstance(comparator, ast.Name)
            and comparator.id == "GROWTH_KIND"
            and isinstance(comparison.left, ast.Attribute)
            and comparison.left.attr == "kind"
        ):
            growth_branches.extend(node.body)
    assert growth_branches, "render_chart must branch on the GROWTH_KIND entry"

    # The branch delegates to a private draw helper; follow the delegation so the
    # guard covers the code that actually issues matplotlib calls.
    drawn_bodies: list[ast.AST] = list(growth_branches)
    for node in growth_branches:
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "_draw_growth"
        ):
            helpers = [
                item
                for item in ast.walk(tree)
                if isinstance(item, ast.FunctionDef) and item.name == "_draw_growth"
            ]
            assert len(helpers) == 1, "the growth render path must be its own function"
            drawn_bodies.extend(helpers[0].body)

    offending = [
        f"{node.func.attr}() at line {node.lineno}"
        for root in drawn_bodies
        for node in ast.walk(root)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in forbidden
    ]
    assert not offending, (
        "the growth branch must name no axis-limit or scale call - a zero floor "
        f"here erases a negative bar: {offending}"
    )

    # The positive half: a zero floor on the *timeseries* path is D-14, and this
    # guard must not mistake it for a violation.
    timeseries_helpers = [
        item
        for item in ast.walk(tree)
        if isinstance(item, ast.FunctionDef) and item.name == "_draw_timeseries"
    ]
    assert len(timeseries_helpers) == 1
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "set_ylim"
        for node in ast.walk(timeseries_helpers[0])
    ), "D-14's timeseries zero floor must still be there - this guard is scoped"


def test_zero_growth_is_a_solid_bar_distinct_from_the_null_bar(tmp_out: Path) -> None:
    """A real 0.0% reading and a null must never be confusable (ANAL-06 as rendered)."""
    _write_growth_pair_with_pct(tmp_out, TRACER_SERIES_ID, "y1", 0.0, 0)
    assert _run_charts(FIXTURES_DIR / "spec.example.json", tmp_out) == 0

    document = _plan(tmp_out, FIXTURES_DIR / "spec.example.json")
    entry = _growth_entry_of(document, TRACER_SERIES_ID)
    bars = {bar.window: bar for bar in entry.bars}
    assert bars["y1"].pct == 0.0
    assert bars["y1"].pct is not None, "a 0.0 reading is a number, never a null"
    assert bars["y1"].reason is None
    assert bars["y2"].pct is None
    assert bars["y2"].reason == "insufficient observations in one or both equal-length windows"

    manifest = json.loads(tmp_out.joinpath("charts.json").read_text(encoding="utf-8"))
    published = _published_growth_of(manifest, TRACER_SERIES_ID)
    published_bars = {bar["window"]: bar for bar in published["bars"]}
    # The distinction is explicit in the artifact, not inferred from a missing key.
    assert published_bars["y1"]["bar_null"] is False
    assert published_bars["y1"]["pct"] == 0.0
    assert "reason" not in published_bars["y1"]
    assert published_bars["y2"]["bar_null"] is True
    assert "pct" not in published_bars["y2"]
    assert published_bars["y2"]["reason"]

    # And a 0.0 bar still has its own place on the value axis: the all-positive
    # range includes it rather than collapsing to a degenerate span.
    assert entry.y_limits[0] < 0.0 <= entry.y_limits[1]


def test_growth_bar_order_matches_contract_key_order(tmp_out: Path) -> None:
    """The bars follow the analyzer's own key order, so it cannot silently reorder."""
    spec_path, _manifest = _render_two_series(tmp_out)
    document = _plan(tmp_out, spec_path)
    for entry in document.charts:
        if entry.kind != make_charts.GROWTH_KIND:
            continue
        assert [bar.window for bar in entry.bars] == list(analyze_trends.GROWTH_WINDOWS)
        assert tuple(bar.window for bar in entry.bars) == make_charts.GROWTH_BAR_WINDOWS
    assert list(analyze_trends.GROWTH_WINDOWS) == ["m3", "y1", "y2"]


# --- Plan 05-03 Task 2: the shared-axis multi-series overlay (D-01, D-20)


def _overlay_entry_of(document: make_charts.ChartDocument) -> make_charts.ChartEntry:
    """The one comparison view a document publishes, by kind rather than by index."""
    overlays = [entry for entry in document.charts if entry.kind == make_charts.OVERLAY_KIND]
    assert len(overlays) == 1, "exactly one comparison view per document"
    return overlays[0]


def test_overlay_shares_axis_and_declares_scale_difference(tmp_out: Path) -> None:
    """D-20: one shared, zero-anchored y-axis and an explicit scales-differ note."""
    spec_path, manifest = _render_two_series(tmp_out)
    document = _plan(tmp_out, spec_path)
    entry = _overlay_entry_of(document)
    metrics = json.loads(tmp_out.joinpath("metrics.json").read_text(encoding="utf-8"))
    order = [node["series_id"] for node in metrics["series"]]
    labels = [node["label"] for node in metrics["series"]]

    # One shared axis: the literal zero floor plus headroom over the largest raw
    # value of *any* series, so no series is scaled away relative to another.
    assert entry.y_limits[0] == 0.0
    largest = max(max(line.raw_values) for line in entry.series_lines)
    assert entry.y_limits[1] >= largest
    assert entry.note == "comparative view; per-series scales differ"
    assert entry.yscale == "linear"

    # Membership is the full metrics series order, and each line keeps its own
    # spec-authored label verbatim.
    assert entry.series_ids == tuple(order)
    assert len(entry.series_lines) == len(order)
    assert [line.series_id for line in entry.series_lines] == order
    assert [line.label for line in entry.series_lines] == labels

    # The two always-emitted text fields resolve to their own sources, never to
    # an invented value: the overlay's own constant label, and the document-level
    # language rather than any single series' language.
    assert entry.label == make_charts.OVERLAY_LABEL
    assert entry.language == document.language
    assert document.language == json.loads(spec_path.read_text(encoding="utf-8"))["language"]
    assert entry.language not in {node["language"] for node in metrics["series"]}
    assert entry.filename == make_charts.OVERLAY_FILENAME
    assert entry.spec_index is None
    assert entry.series_id is None

    # The manifest publishes the overlay the same way, and never a derived copy
    # of the per-series numbers.
    published = next(
        item for item in manifest["charts"] if item["kind"] == make_charts.OVERLAY_KIND
    )
    assert published["series_ids"] == order
    assert published["note"] == make_charts.OVERLAY_NOTE
    assert "series_lines" not in published
    assert "series_id" not in published, "the overlay belongs to no single series"
    assert "spec_index" not in published
    assert document.charts[-1].kind == make_charts.OVERLAY_KIND, "the overlay is last"

    # No axis call in the overlay path may set a log or normalized scale: that
    # would invent a per-series index with no source in metrics.json.
    tree = ast.parse(_module_source())
    overlay_functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_draw_overlay"
    ]
    assert len(overlay_functions) == 1, "the overlay render path must be its own function"
    scale_calls = [
        node
        for node in ast.walk(overlay_functions[0])
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"set_yscale", "set_xscale", "set_rscale"}
    ]
    assert not scale_calls, "D-20 forbids a log or normalized scale on the overlay"


def test_overlay_plots_raw_values_without_rescaling(tmp_out: Path) -> None:
    """The comparison view reuses each series' own numbers, element for element."""
    spec_path, _manifest = _render_two_series(tmp_out)
    document = _plan(tmp_out, spec_path)
    entry = _overlay_entry_of(document)
    per_series = {
        chart.series_id: chart
        for chart in document.charts
        if chart.kind == make_charts.TIMESERIES_KIND
    }

    for line in entry.series_lines:
        own = per_series[line.series_id]
        assert list(line.raw_values) == list(own.raw_values), (
            "the overlay must plot the series' own raw values, not a transformed copy"
        )
        assert list(line.median_values) == list(own.median_values)
        assert [point.date for point in line.points] == [point.date for point in own.points]
        assert line.points == own.points


def test_overlay_note_and_legend_are_rendered_into_the_png(tmp_out: Path) -> None:
    """D-09: the disclosure is visible in the image, and the manifest carries its text."""
    spec_path, manifest = _render_two_series(tmp_out)
    entry = _overlay_entry_of(_plan(tmp_out, spec_path))
    published = next(
        item for item in manifest["charts"] if item["kind"] == make_charts.OVERLAY_KIND
    )

    assert entry.note, "the overlay note must be a non-empty disclosure"
    assert published["note"] == entry.note
    # One legend entry per series, each carrying that series' label verbatim: the
    # per-series names are not lost to the overlay's own constant label.
    assert published["series_ids"] and len(entry.series_lines) == len(entry.series_ids)
    assert all(line.label for line in entry.series_lines)

    blob = tmp_out.joinpath(published["filename"]).read_bytes()
    assert blob[:8] == PNG_MAGIC, "the overlay must be a real PNG"
    assert len(blob) > 1024, "the overlay must not be empty or truncated"


def test_single_series_spec_still_produces_an_overlay(
    tmp_path: Path, tmp_out: Path
) -> None:
    """The comparison view is never conditional on N > 1."""
    spec_path = _write_spec(tmp_path, [_spec_series_item(TRACER_SERIES_ID)])
    _copy_fixtures(tmp_out, (TRACER_SERIES_ID,))
    assert _run_charts(spec_path, tmp_out) == 0

    document = _plan(tmp_out, spec_path)
    # Filtered by kind, never by a total: the count 2N+1 is 05-04's assertion to
    # make, and it must not be this test's fragility.
    assert [c.kind for c in document.charts if c.kind == make_charts.OVERLAY_KIND] == [
        make_charts.OVERLAY_KIND
    ]
    entry = _overlay_entry_of(document)
    assert entry.series_ids == (TRACER_SERIES_ID,)
    assert len(entry.series_lines) == 1
    assert len([c for c in document.charts if c.kind == make_charts.TIMESERIES_KIND]) == 1
    assert tmp_out.joinpath("chart_overlay.png").is_file()


# --- Plan 05-05 Task 1: the mandatory anomaly overlay (D-08, D-09)

GAP_SERIES_ID = "cs-pust-prerusovany"
ANOMALY_DATE = "2026-03-15"
ANOMALY_VALUE = 25380
ANOMALY_MEDIAN = 2539


def _timeseries_entry_of(
    document: make_charts.ChartDocument, series_id: str = TRACER_SERIES_ID
) -> make_charts.ChartEntry:
    """The one timeseries entry a series owns, by kind and id rather than by index."""
    timeseries = [
        entry
        for entry in document.charts
        if entry.kind == make_charts.TIMESERIES_KIND and entry.series_id == series_id
    ]
    assert len(timeseries) == 1, f"exactly one timeseries chart for {series_id}"
    return timeseries[0]


def _legend_labels(entry: make_charts.ChartEntry) -> list[str]:
    """Draw one plan entry on a throwaway axes and read back its legend text.

    The strongest structural statement available about "does this chart claim an
    anomaly": the legend is the only place a chart names its own elements, and
    `ax.get_legend().get_texts()` reads exactly what a reader would read - with
    no dependence on pixels, which are not a stable assertion surface across
    matplotlib/freetype versions.
    """
    import matplotlib

    if matplotlib.get_backend().lower() != "agg":
        matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots()
    try:
        if entry.kind == make_charts.OVERLAY_KIND:
            make_charts._draw_overlay(entry, ax, mdates)
        elif entry.kind == make_charts.GROWTH_KIND:
            make_charts._draw_growth(entry, ax)
        else:
            make_charts._draw_timeseries(entry, ax, mdates)
        legend = ax.get_legend()
        if legend is None:
            return []
        return [text.get_text() for text in legend.get_texts()]
    finally:
        plt.close(fig)


def test_anomaly_segments_match_contract_endpoints(tmp_path: Path) -> None:
    """D-08: the marker's two ends are the contract's own `median` and `value`.

    Element-wise equality with metrics.json's entry is the whole assertion: a
    rescaled, re-rounded or re-estimated endpoint fails here, which is what
    "the chart layer introduces no statistic" means operationally.
    """
    out_dir = tmp_path / "anomalies"
    _write_anomaly_pair(out_dir)
    spec_path = FIXTURES_DIR / "spec.example.json"
    assert _run_charts(spec_path, out_dir) == 0

    document = _plan(out_dir, spec_path)
    metrics = _metrics_by_series(out_dir)
    pl = _timeseries_entry_of(document, TRACER_SERIES_ID)
    cs = _timeseries_entry_of(document, GAP_SERIES_ID)

    # The committed spike pair really does carry a non-empty anomalies[]: the
    # overlay branch is exercised by data, not only by a hand-built document.
    own = next(a for a in metrics[TRACER_SERIES_ID]["anomalies"] if a["date"] == ANOMALY_DATE)
    assert list(pl.anomalies) == [
        {"date": ANOMALY_DATE, "value": ANOMALY_VALUE, "median": ANOMALY_MEDIAN}
    ]
    for field in ("date", "value", "median"):
        assert pl.anomalies[0][field] == own[field], (
            f"the drawn endpoint {field!r} must be metrics.json's own value, verbatim"
        )
    assert pl.anomalies[0]["value"] == ANOMALY_VALUE
    assert pl.anomalies[0]["median"] == ANOMALY_MEDIAN
    assert cs.anomalies == (), "the unspiked series carries no anomaly"

    assert pl.anomalies_drawn == 1
    assert cs.anomalies_drawn == 0

    # The single definition of the field: it is the length of the list it counts,
    # for EVERY entry of EVERY kind - so the two can never drift apart.
    for entry in document.charts:
        assert entry.anomalies_drawn == len(entry.anomalies), (
            f"{entry.kind}/{entry.series_id}: anomalies_drawn must equal len(anomalies)"
        )

    published = next(
        item
        for item in json.loads(out_dir.joinpath("charts.json").read_text(encoding="utf-8"))[
            "charts"
        ]
        if item["kind"] == make_charts.TIMESERIES_KIND
        and item.get("series_id") == TRACER_SERIES_ID
    )
    assert published["anomalies_drawn"] == 1
    # And the chart names the markers it drew: D-09, on the image itself.
    labels = _legend_labels(pl)
    assert make_charts.ANOMALY_LEGEND_LABEL in labels, labels
    assert make_charts.RAW_LINE_LABEL in labels
    assert make_charts.MEDIAN_LINE_LABEL in labels


def test_growth_entry_never_carries_anomalies(tmp_path: Path) -> None:
    """A daily spike on a window-summary chart is a category error, so it never goes there.

    Leaving the growth entry's `anomalies` empty is what makes the single
    `anomalies_drawn == len(anomalies)` definition true for that kind as well:
    its count is 0 for the same reason the rest of its list is.
    """
    out_dir = tmp_path / "anomalies"
    _write_anomaly_pair(out_dir)
    spec_path = FIXTURES_DIR / "spec.example.json"
    assert _run_charts(spec_path, out_dir) == 0

    document = _plan(out_dir, spec_path)
    growth = _growth_entry_of(document, TRACER_SERIES_ID)
    timeseries = _timeseries_entry_of(document, TRACER_SERIES_ID)

    assert growth.anomalies == ()
    assert growth.anomalies_drawn == 0
    assert timeseries.anomalies_drawn == 1
    assert timeseries.anomalies != ()

    # Same series, same document, two kinds: the growth chart's own legend must
    # not claim a marker it does not draw.
    labels = _legend_labels(growth)
    assert make_charts.ANOMALY_LEGEND_LABEL not in labels, labels


def test_empty_anomalies_render_no_markers_and_no_legend_entry(tmp_out: Path) -> None:
    """`anomalies: []` is a valid, common state: zero markers, no anomaly legend entry."""
    spec_path, manifest = _render_two_series(tmp_out)
    document = _plan(tmp_out, spec_path)

    for entry in document.charts:
        assert entry.anomalies == (), f"{entry.kind} must carry no anomaly on the golden"
        assert entry.anomalies_drawn == 0
        assert "anomaly" not in entry.subtitle, (
            "a chart that draws no anomaly must not claim one in its subtitle"
        )
    for item in manifest["charts"]:
        assert item["anomalies_drawn"] == 0

    # The empty state still produces a real chart for every kind.
    for item in manifest["charts"]:
        blob = tmp_out.joinpath(item["filename"]).read_bytes()
        assert blob[:8] == PNG_MAGIC, f"{item['filename']} must be a real PNG"
        assert len(blob) > 1024, f"{item['filename']} must not be empty or truncated"

    for entry in document.charts:
        if entry.kind in {make_charts.TIMESERIES_KIND, make_charts.OVERLAY_KIND}:
            assert make_charts.ANOMALY_LEGEND_LABEL not in _legend_labels(entry)


def _write_gaps_pair(out_dir: Path) -> None:
    """Materialize the committed gap pair, building its metrics with production code.

    The plan does not commit a metrics fixture for the gapless/holey CSV: the
    analyzer is the only sanctioned producer of metrics.json, so the test runs it
    rather than freezing a second hand-maintained copy that could drift.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.joinpath("series.csv").write_bytes(
        FIXTURES_DIR.joinpath("series.gaps.example.csv").read_bytes()
    )
    spec = json.loads(FIXTURES_DIR.joinpath("spec.example.json").read_text(encoding="utf-8"))
    grouped = analyze_trends.load_series_csv(out_dir / "series.csv", spec)
    metrics = analyze_trends.build_metrics(spec, "tests/fixtures/spec.example.json", grouped)
    out_dir.joinpath("metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("anomaly_date", "expected"),
    [
        ("2025-05-12", "not present in series.csv"),
        ("2025-13-45", "anomaly date is not YYYY-MM-DD"),
        ("20250512", "anomaly date is not YYYY-MM-DD"),
    ],
    ids=["absent-day", "impossible-date", "basic-iso-format"],
)
def test_anomaly_date_absent_from_series_csv_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], anomaly_date: str, expected: str
) -> None:
    """T-5-17: a claimed anomaly may not float over a day the chart does not draw.

    2025-05-12 is a real calendar day inside the series' window and inside the
    spec's inclusive bounds, but it is one of the five days the committed gap
    fixture leaves absent - so a marker there would be a claim the picture
    cannot support. The other two cases pin the format guard: Python 3.11's
    `date.fromisoformat` also accepts the basic "20250512" form, so the
    YYYY-MM-DD check cannot be delegated to it.
    """
    out_dir = tmp_path / "gaps"
    _write_gaps_pair(out_dir)
    path = out_dir / "metrics.json"
    metrics = json.loads(path.read_text(encoding="utf-8"))
    node = next(n for n in metrics["series"] if n["series_id"] == GAP_SERIES_ID)
    node["anomalies"] = [{"date": anomaly_date, "value": 9999, "median": 1200}]
    path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    assert _run_charts(FIXTURES_DIR / "spec.example.json", out_dir) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("charts failed:"), captured.err
    assert expected in captured.err, captured.err
    assert anomaly_date in captured.err
    assert list(out_dir.glob("*.png")) == [], "a refused anomaly must write no PNG"
    assert not out_dir.joinpath("charts.json").exists()


def test_anomaly_missing_field_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A half-read anomaly is refused rather than drawn as a partial marker."""
    out_dir = tmp_path / "anomalies"
    _write_anomaly_pair(out_dir)
    path = out_dir / "metrics.json"
    metrics = json.loads(path.read_text(encoding="utf-8"))
    node = next(n for n in metrics["series"] if n["series_id"] == TRACER_SERIES_ID)
    del node["anomalies"][0]["median"]
    path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    assert _run_charts(FIXTURES_DIR / "spec.example.json", out_dir) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("charts failed:"), captured.err
    assert "anomaly is missing 'median'" in captured.err
    assert TRACER_SERIES_ID in captured.err
    assert list(out_dir.glob("*.png")) == []


def test_float_median_is_not_truncated(tmp_path: Path) -> None:
    """T-5-18: `statistics.median` returns a float on an even-length window.

    RESEARCH Pattern 4 recorded a real one from a live capture:
    `{"date": "2025-09-21", "value": 2586, "median": 2189.5}`. A narrowing cast
    to int would silently move a drawn coordinate by half a view - invisible in
    the PNG and fatal to the contract's promise that the marker is the number.
    """
    out_dir = tmp_path / "anomalies"
    _write_anomaly_pair(out_dir)
    path = out_dir / "metrics.json"
    metrics = json.loads(path.read_text(encoding="utf-8"))
    node = next(n for n in metrics["series"] if n["series_id"] == TRACER_SERIES_ID)
    node["anomalies"] = [{"date": "2025-09-21", "value": 2586, "median": 2189.5}]
    path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    spec_path = FIXTURES_DIR / "spec.example.json"
    assert _run_charts(spec_path, out_dir) == 0
    entry = _timeseries_entry_of(_plan(out_dir, spec_path))

    assert entry.anomalies_drawn == 1
    assert entry.anomalies[0]["median"] == 2189.5
    assert isinstance(entry.anomalies[0]["median"], float)
    assert entry.anomalies[0]["value"] == 2586
    assert isinstance(entry.anomalies[0]["value"], int)


def test_zero_length_anomaly_segment_still_gets_a_marker(tmp_path: Path) -> None:
    """A `value == median` marker is a zero-length segment: it would draw nothing.

    The detector can never emit a zero score at MAD_K = 3.5, so this only arises
    from a hand-built document - but "a claimed anomaly that renders as nothing"
    is a lie by omission, so the marker point is unconditional on the equality.
    """
    out_dir = tmp_path / "anomalies"
    _write_anomaly_pair(out_dir)
    path = out_dir / "metrics.json"
    metrics = json.loads(path.read_text(encoding="utf-8"))
    node = next(n for n in metrics["series"] if n["series_id"] == TRACER_SERIES_ID)
    node["anomalies"] = [{"date": ANOMALY_DATE, "value": 2539, "median": 2539}]
    path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    spec_path = FIXTURES_DIR / "spec.example.json"
    assert _run_charts(spec_path, out_dir) == 0
    entry = _timeseries_entry_of(_plan(out_dir, spec_path))
    assert entry.anomalies[0]["value"] == entry.anomalies[0]["median"]

    import matplotlib

    if matplotlib.get_backend().lower() != "agg":
        matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots()
    try:
        make_charts._draw_timeseries(entry, ax, mdates)
        drawn = [line for line in ax.get_lines() if line.get_marker() == "o"]
        assert drawn, "a zero-length anomaly must still get a visible marker point"
        legend = ax.get_legend()
        labels = [text.get_text() for text in legend.get_texts()] if legend else []
        assert make_charts.ANOMALY_LEGEND_LABEL in labels, labels
    finally:
        plt.close(fig)


def test_chart_subtitle_and_legend_name_their_method(tmp_path: Path) -> None:
    """D-09: a PNG separated from the report still says what it shows."""
    out_dir = tmp_path / "anomalies"
    _write_anomaly_pair(out_dir)
    spec_path = FIXTURES_DIR / "spec.example.json"
    assert _run_charts(spec_path, out_dir) == 0

    document = _plan(out_dir, spec_path)
    kinds: set[str] = set()
    for entry in document.charts:
        kinds.add(entry.kind)
        assert isinstance(entry.subtitle, str) and entry.subtitle.strip()
        if entry.kind == make_charts.TIMESERIES_KIND:
            assert "raw" in entry.subtitle
            assert "median" in entry.subtitle
            if entry.anomalies_drawn > 0:
                assert "anomaly" in entry.subtitle
        if entry.kind == make_charts.GROWTH_KIND:
            assert "clean" in entry.subtitle
    assert kinds == {
        make_charts.TIMESERIES_KIND,
        make_charts.GROWTH_KIND,
        make_charts.OVERLAY_KIND,
    }, "every kind must be asserted, so the test cannot pass vacuously"

    published = json.loads(out_dir.joinpath("charts.json").read_text(encoding="utf-8"))
    for item in published["charts"]:
        assert item["subtitle"] == next(
            entry.subtitle for entry in document.charts if entry.filename == item["filename"]
        ), "the manifest must publish the subtitle the image carries"
        assert item["subtitle"]


# --- Plan 05-05 Task 2: calendar gaps as a break and a labelled band (D-10..D-13)

GAP_START = "2025-05-10"
GAP_END = "2025-05-14"
GAP_DAYS = 5


def _gap_objects(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Every gap record the manifest publishes, across every entry."""
    return [gap for entry in manifest["charts"] for gap in entry["gaps"]]


def _run_gap_pair(tmp_path: Path) -> tuple[Path, Path]:
    """Materialize the committed gap pair and run the CLI over it."""
    out_dir = tmp_path / "gaps"
    _write_gaps_pair(out_dir)
    spec_path = FIXTURES_DIR / "spec.example.json"
    assert _run_charts(spec_path, out_dir) == 0
    return out_dir, spec_path


def test_gaps_are_per_series_and_rendered(tmp_path: Path) -> None:
    """D-12/D-13: one series' internal hole is that series' band, and only that one.

    RESEARCH Pitfall 4 measured the failure this guards: `series.csv` is sorted
    by `(series_id, date)`, so a gap walk over the raw file order reports the
    sort boundary between two complete series as a 365-day absence.
    """
    out_dir, spec_path = _run_gap_pair(tmp_path)
    document = _plan(out_dir, spec_path)
    manifest = json.loads(out_dir.joinpath("charts.json").read_text(encoding="utf-8"))

    pl = _timeseries_entry_of(document, TRACER_SERIES_ID)
    cs = _timeseries_entry_of(document, GAP_SERIES_ID)
    assert pl.gaps == (), "the complete series must have no gap at all"
    assert len(cs.gaps) == 1, "the holed series has exactly one internal gap"
    gap = cs.gaps[0]
    assert gap.series_id == GAP_SERIES_ID
    assert gap.start.isoformat() == GAP_START
    assert gap.end.isoformat() == GAP_END
    assert gap.days == GAP_DAYS

    objects = _gap_objects(manifest)
    assert len(objects) == 2, "one record on the holed series' own entry and on the overlay"
    assert {record["series_id"] for record in objects} == {GAP_SERIES_ID}
    for record in objects:
        assert set(record) == {"series_id", "start", "end", "days"}
        assert record["start"] == GAP_START
        assert record["end"] == GAP_END
        assert record["days"] == GAP_DAYS
        assert record["days"] == (
            date.fromisoformat(record["end"]) - date.fromisoformat(record["start"])
        ).days + 1

    # The chart the band belongs to is a real PNG, and so is the one that has no
    # band to draw.
    for record in objects:
        blob = out_dir.joinpath(f"chart_{GAP_SERIES_ID}_timeseries.png").read_bytes()
        assert blob[:8] == PNG_MAGIC
        assert len(blob) > 1024, "the gapped chart must not be empty or truncated"
    published = next(
        item
        for item in manifest["charts"]
        if item["kind"] == make_charts.TIMESERIES_KIND
        and item.get("series_id") == GAP_SERIES_ID
    )
    assert published["gaps"] == objects[0:1] or published["gaps"] == [objects[0]]

    # D-10: the x-axis spans the series' own full calendar range, so the absence
    # is expressed as a break in the line, never as a compressed axis.
    assert pl.x_limits == (date(2024, 9, 23), date(2026, 9, 20))
    assert cs.x_limits == pl.x_limits
    overlay = _overlay_entry_of(document)
    assert overlay.x_limits == pl.x_limits


def test_multi_series_csv_produces_no_spurious_band(tmp_out: Path) -> None:
    """The Pitfall 4 regression: a gapless two-series CSV yields zero gap records.

    This is the exact shape that previously reported a fictitious 365-day hole:
    both series complete, concatenated in `(series_id, date)` order. If gap
    detection ever walks the file order instead of one series' own dates, this
    fails.
    """
    spec_path, manifest = _render_two_series(tmp_out)
    document = _plan(tmp_out, spec_path)

    for entry in document.charts:
        assert entry.gaps == (), (
            f"{entry.kind}/{entry.series_id}: a complete series has no gap, and the "
            "boundary between two series is not one"
        )
    assert _gap_objects(manifest) == []
    for line in _overlay_entry_of(document).series_lines:
        assert line.gaps == ()


def test_absent_day_breaks_the_line_and_is_not_bridged(tmp_path: Path) -> None:
    """D-12: an absent calendar day is a break, never a bridge.

    A bridging implementation inserts the five missing days (or interpolates
    across them), so the point count is the assertion that bites: the holed
    series must be exactly five observations short of the complete one.
    """
    out_dir, spec_path = _run_gap_pair(tmp_path)
    document = _plan(out_dir, spec_path)
    pl = _timeseries_entry_of(document, TRACER_SERIES_ID)
    cs = _timeseries_entry_of(document, GAP_SERIES_ID)

    assert len(cs.points) == len(pl.points) - GAP_DAYS
    missing = (date.fromisoformat(GAP_START), date.fromisoformat(GAP_END))
    for point in cs.points:
        assert not (missing[0] <= point.date <= missing[1]), (
            f"no plotted point may exist inside the declared absence: {point.date}"
        )
    # The plotted sequences stay aligned with the points, and neither the raw nor
    # the median line was padded to cover the hole.
    assert len(cs.raw_values) == len(cs.points)
    assert len(cs.median_values) == len(cs.points)
    # And the line really is broken: the render path injects one NaN per absent
    # day into BOTH sequences, which is the only permitted way to express one.
    import matplotlib

    if matplotlib.get_backend().lower() != "agg":
        matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots()
    try:
        make_charts._draw_timeseries(cs, ax, mdates)
        raw, median_line = ax.get_lines()[0], ax.get_lines()[1]
        raw_ydata = list(raw.get_ydata())
        median_ydata = list(median_line.get_ydata())
        assert len(raw_ydata) == len(cs.points) + GAP_DAYS, (
            "the raw sequence must carry one NaN per absent day"
        )
        assert len(median_ydata) == len(cs.points) + GAP_DAYS
        nan_positions = [
            index for index, value in enumerate(raw_ydata) if value != value  # NaN != NaN
        ]
        assert len(nan_positions) == GAP_DAYS
        bands = [patch for patch in ax.patches if patch.get_label() == make_charts.GAP_BAND_LABEL]
        assert len(bands) == 1, "exactly one 'no data' band for the one gap"
    finally:
        plt.close(fig)


def test_zero_views_row_is_plotted_not_dropped(tmp_path: Path) -> None:
    """D-11: a `views=0` row is a reading, not an absence to be second-guessed.

    This is the null-vs-zero boundary rendered. A zero is a real observation, so
    it stays a plotted point at y=0 and is covered by no gap object; dropping it
    or turning it into a break would let the chart invent a data outage that
    fetch_pageviews.py never reported.
    """
    out_dir = tmp_path / "zero"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = FIXTURES_DIR.joinpath("series.example.csv").read_text(encoding="utf-8").splitlines()
    patched: list[str] = []
    seen = 0
    for index, line in enumerate(rows):
        if index > 0 and line.startswith("2025-01-15,") and ",pl-post-przerywany," in line:
            fields = line.split(",")
            fields[1] = "0"
            line = ",".join(fields)
            seen += 1
        patched.append(line)
    assert seen == 1, f"expected exactly one pl row on 2025-01-15, found {seen}"
    out_dir.joinpath("series.csv").write_text("\n".join(patched) + "\n", encoding="utf-8")

    spec_path = FIXTURES_DIR / "spec.example.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    grouped = analyze_trends.load_series_csv(out_dir / "series.csv", spec)
    metrics = analyze_trends.build_metrics(spec, str(spec_path), grouped)
    out_dir.joinpath("metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    assert _run_charts(spec_path, out_dir) == 0

    document = _plan(out_dir, spec_path)
    entry = _timeseries_entry_of(document, TRACER_SERIES_ID)
    zero_points = [point for point in entry.points if point.date == date(2025, 1, 15)]
    assert len(zero_points) == 1, "the zero day must still be a plotted point"
    assert zero_points[0].views == 0
    assert zero_points[0].views in entry.raw_values
    assert entry.gaps == (), "a zero is a reading; it is never a gap"
    for gap in _gap_objects(json.loads(out_dir.joinpath("charts.json").read_text(encoding="utf-8"))):
        assert not (
            date.fromisoformat(gap["start"]) <= date(2025, 1, 15) <= date.fromisoformat(gap["end"])
        ), "no gap object may cover a real zero reading"

    # The unpatched series is untouched, so the edit was local to one cell.
    cs = _timeseries_entry_of(document, GAP_SERIES_ID)
    assert [point.views for point in cs.points if point.date == date(2025, 1, 15)] != [0]


def test_gap_annotation_text_is_in_the_manifest(tmp_path: Path) -> None:
    """D-13: the band carries the exact text a reader sees, asserted against a literal.

    "no data <start>..<end>" is the string that stops a break being read as the
    end of the history, so its format is pinned rather than pattern-matched: a
    separator change or a dropped range is a defect a reader would see and a
    loose assertion would not.
    """
    out_dir, spec_path = _run_gap_pair(tmp_path)
    document = _plan(out_dir, spec_path)
    cs = _timeseries_entry_of(document, GAP_SERIES_ID)
    gap = cs.gaps[0]

    assert gap.label == f"no data {GAP_START}..{GAP_END}"
    assert gap.label == "no data 2025-05-10..2025-05-14"
    assert make_charts.gap_annotation_text(
        date.fromisoformat(GAP_START), date.fromisoformat(GAP_END)
    ) == "no data 2025-05-10..2025-05-14"
    assert make_charts.GAP_BAND_LABEL == "no data"

    # The render path draws the band's own text, so the plan object and the image
    # cannot disagree about what the absence is called.
    import matplotlib

    if matplotlib.get_backend().lower() != "agg":
        matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots()
    try:
        make_charts._draw_timeseries(cs, ax, mdates)
        drawn = [text.get_text() for text in ax.texts]
        assert "no data 2025-05-10..2025-05-14" in drawn, drawn
        legend = ax.get_legend()
        labels = [text.get_text() for text in legend.get_texts()] if legend else []
        assert make_charts.GAP_BAND_LABEL in labels, labels
    finally:
        plt.close(fig)

    # Every published record carries the full quadruple, and no gap object is
    # ever nulled under the manifest's no-null rule.
    for record in _gap_objects(
        json.loads(out_dir.joinpath("charts.json").read_text(encoding="utf-8"))
    ):
        assert record["series_id"]
        assert record["start"] and record["end"]
        assert isinstance(record["days"], int) and record["days"] > 0


def test_truncated_median_window_at_a_series_edge_is_not_a_gap(tmp_out: Path) -> None:
    """A short local window is not an absence: the underlying days are present.

    `rolling_median_7` truncates its window at the first and last point of a
    series. Under D-12 that must produce no break and no band, because the days
    behind the short window ARE in series.csv.
    """
    spec_path, manifest = _render_two_series(tmp_out)
    document = _plan(tmp_out, spec_path)
    for entry in document.charts:
        assert entry.gaps == ()
        assert len(entry.median_values) == len(entry.points) or entry.kind == make_charts.GROWTH_KIND
    assert _gap_objects(manifest) == []
    first = _timeseries_entry_of(document, TRACER_SERIES_ID)
    assert len(first.median_values) == len(first.points)


def test_gap_text_stays_inside_the_canvas_even_at_a_series_edge(tmp_path: Path) -> None:
    """A band at the very start or end of a history must not push its label off the image.

    This is a legibility defect turned into a structural guard. The first render
    of the "no data <range>" annotation centred the text on its band, so a hole
    in the first sixteen days ran "no data 2024-09-24..2024-10-09" off the LEFT
    edge of the PNG and a hole in the last sixteen ran the other one off the
    RIGHT. Both shipped through a green suite: the only thing that could see it
    was opening the image, which is why this test now measures the text's own
    window extent against the figure's and needs no human.
    """
    out_dir = tmp_path / "edges"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = list(csv.DictReader(FIXTURES_DIR.joinpath("series.example.csv").open(encoding="utf-8")))
    final_day = date(2026, 9, 20)
    first_day = date(2024, 9, 23)
    early = {first_day + timedelta(days=offset) for offset in range(1, 17)}
    late = {final_day - timedelta(days=offset) for offset in range(1, 17)}
    kept = [
        row
        for row in rows
        if not (
            row["series_id"] == GAP_SERIES_ID
            and date.fromisoformat(row["date"]) in early | late
        )
    ]
    with out_dir.joinpath("series.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["date", "views", "series_id", "project", "article"],
            lineterminator="\r\n",
        )
        writer.writeheader()
        writer.writerows(kept)

    spec_path = FIXTURES_DIR / "spec.example.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    grouped = analyze_trends.load_series_csv(out_dir / "series.csv", spec)
    out_dir.joinpath("metrics.json").write_text(
        json.dumps(
            analyze_trends.build_metrics(spec, str(spec_path), grouped),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    assert _run_charts(spec_path, out_dir) == 0

    document = _plan(out_dir, spec_path)
    cs = _timeseries_entry_of(document, GAP_SERIES_ID)
    assert len(cs.gaps) == 2, "a hole at each end of the history"
    assert cs.gaps[0].start == date(2024, 9, 24)
    assert cs.gaps[1].end == date(2026, 9, 19)

    import matplotlib

    if matplotlib.get_backend().lower() != "agg":
        matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=make_charts.FIGURESIZE, dpi=make_charts.DPI)
    try:
        make_charts._draw_timeseries(cs, ax, mdates)
        fig.canvas.draw()
        width, height = fig.canvas.get_width_height()
        assert [text.get_text() for text in ax.texts] == [
            "no data 2024-09-24..2024-10-09",
            "no data 2026-09-04..2026-09-19",
        ]
        for artist in ax.texts:
            left, bottom, right, top = artist.get_window_extent().bounds
            assert left >= 0, f"{artist.get_text()!r} runs off the left edge ({left:.1f})"
            assert right <= width, f"{artist.get_text()!r} runs off the right edge ({right:.1f} of {width})"
            assert top <= height, f"{artist.get_text()!r} runs off the top ({top:.1f} of {height})"
            assert bottom >= 0, f"{artist.get_text()!r} runs off the bottom ({bottom:.1f})"
        # And one legend row, not one per band: two identical "no data" entries
        # is noise, and the count of absences is already in the manifest.
        legend = ax.get_legend()
        labels = [text.get_text() for text in legend.get_texts()] if legend else []
        assert labels.count(make_charts.GAP_BAND_LABEL) == 1, labels
    finally:
        plt.close(fig)

