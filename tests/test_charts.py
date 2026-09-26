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
    # N=1 publishes the timeseries plus the comparison view (D-19: 2N+1), and at
    # N=1 that is 2 of the 5 a two-kind inventory would reach at N=2.
    pngs = sorted(tmp_out.glob("*.png"))
    assert len(pngs) == 2, f"expected the timeseries and the overlay, got {pngs}"
    for png in pngs:
        blob = png.read_bytes()
        assert blob[:8] == PNG_MAGIC, "rendered chart must be a real PNG"
        assert len(blob) > 1024, "rendered chart must not be empty or truncated"

    manifest = json.loads(tmp_out.joinpath("charts.json").read_text(encoding="utf-8"))
    assert manifest["contract_version"] == "charts.v1"
    assert manifest["spec_name"] == "intermittent_fasting_pl_cs"
    assert manifest["language"] == "uk"
    assert len(manifest["charts"]) == 2
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
    """D-19: one PNG per (series_id, kind) pair, plus exactly one overlay (2N+1)."""
    spec_path, manifest = _render_two_series(tmp_out)
    document = _plan(tmp_out, spec_path)
    order = _metrics_series_order()
    per_series = [entry for entry in document.charts if entry.series_id is not None]
    overlay = [entry for entry in document.charts if entry.kind == make_charts.OVERLAY_KIND]
    timeseries = [entry for entry in per_series if entry.kind == make_charts.TIMESERIES_KIND]

    # One timeseries entry per series, in metrics.json series order. CONTRACTS.md
    # §3 freezes that order as spec.series[] order, so nothing may sort it away.
    assert [entry.series_id for entry in timeseries] == list(order)
    assert [entry.spec_index for entry in timeseries] == list(range(len(order)))
    assert [entry.spec_index for entry in per_series] == sorted(
        entry.spec_index for entry in per_series
    ), "charts[] must follow spec order, never a sort by label or language"

    # A chart is identified by the pair (series_id, kind): no duplicate pair, and
    # the per-series side of the inventory is exactly N x (kinds per series).
    pairs = [(entry.series_id, entry.kind) for entry in per_series]
    assert len(pairs) == len(set(pairs)), "one entry per (series_id, kind) pair"
    kinds_per_series = {entry.kind for entry in per_series}
    assert len(per_series) == len(order) * len(kinds_per_series)
    assert len(overlay) == 1, "the comparison view must never be conditional on N > 1"
    # D-19's 2N+1: N x (per-series kinds) + exactly one comparison view. With the
    # growth kind of 05-04 this is the literal 2N+1; today it is 2 + 1 = 3.
    assert len(document.charts) == len(order) * len(kinds_per_series) + 1
    assert len(document.charts) == 3
    assert document.charts[-1].kind == make_charts.OVERLAY_KIND, "the overlay is last"

    # Count from the manifest, never from a directory glob: an orphan file must
    # not be able to stand in for a designed chart, and a dangling entry must
    # not be able to hide.
    assert len(manifest["charts"]) == len(document.charts)
    assert sorted(path.name for path in tmp_out.glob("*.png")) == sorted(
        entry["filename"] for entry in manifest["charts"]
    )
    assert [entry["spec_index"] for entry in manifest["charts"] if "series_id" in entry] == list(
        range(len(order))
    )


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
        chart.series_id: chart for chart in document.charts if chart.series_id is not None
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
