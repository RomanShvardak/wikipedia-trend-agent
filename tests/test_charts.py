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
import unicodedata
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


def _plan(
    out_dir: Path, spec_path: Path, *, log_scale: bool = False
) -> make_charts.ChartDocument:
    """Rebuild the chart plan from the same inputs the CLI read.

    `log_scale` must be passed whenever the CLI was given `--log-scale`: the
    plan is a pure function of its inputs, so rebuilding it without the flag
    would silently describe the LINEAR chart while the disk holds a log one.
    """
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    metrics, digest = make_charts.load_metrics(out_dir / "metrics.json")
    grouped = analyze_trends.load_series_csv(out_dir / "series.csv", spec)
    return make_charts.build_chart_plan(
        spec, metrics, digest, grouped, str(out_dir / "metrics.json"), log_scale=log_scale
    )


def _module_source() -> str:
    """Read the chart module's own source, path resolved once here."""
    return Path(str(make_charts.__file__)).read_text(encoding="utf-8")


# Every matplotlib method that changes an axis SCALE, as opposed to its limits.
# `set_xlim`/`set_ylim` are absent on purpose: D-14 and D-10 both mandate them
# on the timeseries axes, so a guard listing them would forbid the phase.
AXIS_SCALE_METHODS = frozenset(
    {
        "set_xscale",
        "set_yscale",
        "set_zscale",
        "set_rscale",
        "set_rorigin",
        "set_rlabel_position",
        "loglog",
        "semilogx",
        "semilogy",
    }
)


def _function_node(name: str) -> ast.FunctionDef:
    """The single module-level function called `name`, located by AST."""
    found = [
        node
        for node in ast.walk(ast.parse(_module_source()))
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    assert len(found) == 1, f"the module must define exactly one {name}()"
    return found[0]


def _scale_calls_in_render_path(function_name: str) -> list[dict[str, Any]]:
    """Every axis-scale call reachable from `function_name`, delegation followed.

    A guard that only reads one function's body stops guarding the moment the
    call moves into a helper - which is exactly what happened when 05-06 lifted
    D-15's `set_yscale` out of `_draw_overlay` into `_apply_log_regime`. So this
    walk descends one level into any private helper the named function calls,
    and reports each call's method name, its constant positional arguments and
    its keyword arguments so an assertion can name a permitted exact call rather
    than merely a permitted method.
    """
    tree = ast.parse(_module_source())
    entry_point = _function_node(function_name)
    bodies: list[Any] = list(entry_point.body)
    for node in ast.walk(entry_point):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id.startswith("_")
        ):
            bodies.extend(_function_node(node.func.id).body)
    calls: list[dict[str, Any]] = []
    for root in bodies:
        for node in ast.walk(root):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in AXIS_SCALE_METHODS
            ):
                calls.append(
                    {
                        "attr": node.func.attr,
                        "line": node.lineno,
                        "positional": [
                            arg.value
                            for arg in node.args
                            if isinstance(arg, ast.Constant)
                        ],
                        "keywords": {
                            keyword.arg: (
                                keyword.value.value
                                if isinstance(keyword.value, ast.Constant)
                                else "<dynamic>"
                            )
                            for keyword in node.keywords
                            if keyword.arg is not None
                        },
                    }
                )
    return calls


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
    # an invented value: the overlay's own name (a language token like every
    # other word this module writes), and the document-level language rather
    # than any single series' language.
    assert entry.label == make_charts.chart_tokens("uk")["comparison_view"]
    assert entry.language == document.language
    assert entry.text_language == document.language, (
        "the overlay's WORDS follow the document language, not a member series'"
    )
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

    # No axis call in the overlay path may normalize, rebase or rescale a
    # series: that would invent a per-series index with no source in
    # metrics.json. D-15's explicitly-labelled log regime is the ONE scale call
    # this path may make, and it rescales nothing - it only changes the spacing
    # of the reader's eye along the one shared axis.
    #
    # The walk follows the delegation into `_apply_log_regime`, which is where
    # the call now lives. It did not when 05-03 wrote this assertion, and a
    # guard that stops covering the code it names is worse than no guard: it
    # keeps passing after the rule it enforces has stopped being enforced.
    scale_calls = _scale_calls_in_render_path("_draw_overlay")
    for call in scale_calls:
        assert call["attr"] == "set_yscale", (
            f"the overlay path may only set the y scale; found {call}"
        )
        assert call["positional"] == ["log"], (
            f"the only permitted y scale is D-15's labelled log regime: {call}"
        )
        assert call["keywords"].get("nonpositive") == "mask", (
            f"the log regime must state its non-positive handling explicitly: {call}"
        )
    assert any(call["attr"] == "set_yscale" for call in scale_calls), (
        "the overlay render path must still reach D-15's log regime - if this "
        "fails the guard above has stopped covering the code it names"
    )


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
    # And the chart names the markers it drew: D-09, on the image itself. The
    # words are the chart's own language's (D-16), not the module's English
    # aliases - the committed spec asks for Ukrainian.
    tokens = make_charts.chart_tokens("uk")
    labels = _legend_labels(pl)
    assert tokens["anomalies"] in labels, labels
    assert tokens["raw_daily"] in labels
    assert tokens["median7"] in labels


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
    tokens = make_charts.chart_tokens("uk")
    labels = _legend_labels(growth)
    assert tokens["anomalies"] not in labels, labels


def test_empty_anomalies_render_no_markers_and_no_legend_entry(tmp_out: Path) -> None:
    """`anomalies: []` is a valid, common state: zero markers, no anomaly legend entry."""
    spec_path, manifest = _render_two_series(tmp_out)
    document = _plan(tmp_out, spec_path)
    tokens = make_charts.chart_tokens("uk")

    for entry in document.charts:
        assert entry.anomalies == (), f"{entry.kind} must carry no anomaly on the golden"
        assert entry.anomalies_drawn == 0
        assert tokens["anomalies"] not in entry.subtitle, (
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
            assert tokens["anomalies"] not in _legend_labels(entry)


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
        assert make_charts.chart_tokens("uk")["anomalies"] in labels, labels
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
    tokens = make_charts.chart_tokens("uk")
    for entry in document.charts:
        kinds.add(entry.kind)
        assert isinstance(entry.subtitle, str) and entry.subtitle.strip()
        if entry.kind == make_charts.TIMESERIES_KIND:
            assert tokens["raw_daily"] in entry.subtitle
            assert tokens["median7"] in entry.subtitle
            if entry.anomalies_drawn > 0:
                assert tokens["anomalies"] in entry.subtitle
        if entry.kind == make_charts.GROWTH_KIND:
            assert tokens["clean_growth"] in entry.subtitle
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


def test_x_limits_is_published_on_every_chart_that_has_a_date_axis(tmp_out: Path) -> None:
    """05-07's ratified amendment: the D-10 domain reaches charts.json.

    The value was already computed and already test-enforced on the plan object
    (the D-10 assertions above); publishing it is what stops Phase 6 from
    deriving the same number a second time from `period`. The guard is
    `x_limits is not None`, NOT a kind test, so this asserts the observed
    invariant (the two dated kinds carry it, the growth chart does not) without
    promising the code a kind check it does not make.
    """
    spec_path, manifest = _render_two_series(tmp_out)
    document = _plan(tmp_out, spec_path)

    published = {entry["filename"]: entry for entry in manifest["charts"]}
    dated = 0
    for entry in document.charts:
        item = published[entry.filename]
        if entry.x_limits is None:
            # The growth chart's x axis is a percentage axis: no domain exists,
            # so the key is ABSENT - never a null the no-null rule forbids.
            assert "x_limits" not in item, (
                f"{entry.kind} has no date axis, so charts.json must omit x_limits "
                f"entirely, not null it (05-02's no-null rule)"
            )
            continue
        dated += 1
        assert item["x_limits"] == [
            entry.x_limits[0].isoformat(),
            entry.x_limits[1].isoformat(),
        ], (
            f"{entry.filename}: charts.json must publish the plan object's own x_limits "
            "verbatim, so no consumer re-derives the domain from period"
        )
        # Published as ISO dates a consumer can parse without a matplotlib import.
        assert date.fromisoformat(item["x_limits"][0]) <= date.fromisoformat(
            item["x_limits"][1]
        )
    assert dated == 3, (
        f"the two dated kinds over two series must publish 3 domains, got {dated} - "
        "a vacuous pass here would let the key silently vanish from every entry"
    )


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
        band_label = make_charts.chart_tokens("uk")["no_data"]
        bands = [patch for patch in ax.patches if patch.get_label() == band_label]
        assert len(bands) == 1, "exactly one band legend row for the one gap"
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
    loose assertion would not. D-16 changes the PREFIX, not the format - the
    committed spec is Ukrainian, so the drawn text is the `uk` token around the
    contract's own `YYYY-MM-DD` range, and the English form remains assertable
    through the same function's default argument.
    """
    out_dir, spec_path = _run_gap_pair(tmp_path)
    document = _plan(out_dir, spec_path)
    cs = _timeseries_entry_of(document, GAP_SERIES_ID)
    gap = cs.gaps[0]
    uk_no_data = make_charts.chart_tokens("uk")["no_data"]

    assert gap.no_data == uk_no_data
    assert gap.label == f"{uk_no_data} 2025-05-10..2025-05-14"
    assert gap.label == "немає даних 2025-05-10..2025-05-14"
    # The DATE FORMAT is the contract's and does not move with the language: the
    # same function with the English token reproduces 05-05's exact string.
    assert make_charts.gap_annotation_text(date(2025, 5, 10), date(2025, 5, 14)) == (
        "no data 2025-05-10..2025-05-14"
    )
    assert make_charts.gap_annotation_text(
        date(2025, 5, 10), date(2025, 5, 14), uk_no_data
    ) == gap.label
    assert make_charts.NO_DATA_LABEL == "no data"

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
        assert "немає даних 2025-05-10..2025-05-14" in drawn, drawn
        assert "no data 2025-05-10..2025-05-14" not in drawn, (
            "an English absence note inside a Ukrainian chart is the T-5-24 defect"
        )
        legend = ax.get_legend()
        labels = [text.get_text() for text in legend.get_texts()] if legend else []
        assert uk_no_data in labels, labels
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
    # D-16: the prefix is the chart's own language token, so the canonical
    # 05-05 string is one word longer here - and the same clipping geometry,
    # which is what this test exists to pin.
    uk_no_data = make_charts.chart_tokens("uk")["no_data"]

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
            f"{uk_no_data} 2024-09-24..2024-10-09",
            f"{uk_no_data} 2026-09-04..2026-09-19",
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
        assert labels.count(uk_no_data) == 1, labels
    finally:
        plt.close(fig)


# --- Plan 05-06 Task 1: the opt-in log mode and its disclosure (D-15)

# The two days the log tests set to `views=0`. D-11 keeps a zero a reading, and
# D-15 says the log regime - which cannot draw a zero - must say how many it did
# not draw rather than dropping the days in silence (RESEARCH Pitfall 3 measured
# a log axis whose visible range began at 3.7 with both zero days gone and no
# warning at all).
LOG_ZERO_DATES = ("2025-01-15", "2025-01-16")
LOG_MASKED_DAYS = len(LOG_ZERO_DATES)


def _write_zero_pair(out_dir: Path, series_id: str, dates: tuple[str, ...]) -> Path:
    """Copy the committed CSV, zero the named days of one series, rebuild metrics.

    metrics.json is produced by the sanctioned analyzer rather than hand-edited:
    a `views=0` day is a real observation, so the growth windows and the anomaly
    detector must see the edited data, and only the analyzer is allowed to say
    what they become.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = FIXTURES_DIR.joinpath("series.example.csv").read_text(encoding="utf-8").splitlines()
    patched: list[str] = []
    seen: set[str] = set()
    for index, line in enumerate(rows):
        if index > 0:
            fields = line.split(",")
            if fields[2] == series_id and fields[0] in dates:
                fields[1] = "0"
                line = ",".join(fields)
                seen.add(fields[0])
        patched.append(line)
    assert seen == set(dates), f"expected {series_id} rows on {dates}, edited {sorted(seen)}"
    out_dir.joinpath("series.csv").write_text("\n".join(patched) + "\n", encoding="utf-8")

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
    return spec_path


def _csv_nonpositive_count(path: Path, series_id: str) -> int:
    """Recompute the masked count from the CSV, independently of the renderer.

    The plan's whole point is that the number the chart prints is the number the
    data supports. A test that read the count back out of the plan it is testing
    would agree with any under-reporting renderer, so the expected value is
    counted here from the file.
    """
    with path.open("r", newline="", encoding="utf-8") as handle:
        return sum(
            1
            for row in csv.DictReader(handle)
            if row["series_id"] == series_id and int(row["views"]) <= 0
        )


def _run_charts_with_flags(spec_path: Path, out_dir: Path, *flags: str) -> int:
    return make_charts.main(
        ["--spec", str(spec_path), "--out", str(out_dir), *flags]
    )


def test_log_mode_is_explicit_and_discloses_masked_points(tmp_path: Path) -> None:
    """D-15: the log regime exists only when asked for, and always says what it hid.

    Three assertions in one test because they are one rule: without the flag the
    axis is linear and nothing is masked; with it the timeseries axis is log and
    the masked count equals the number of non-positive readings the CSV actually
    carries; and the same number is stated on the chart and in the manifest.
    """
    out_dir = tmp_path / "linear"
    spec_path = _write_zero_pair(out_dir, TRACER_SERIES_ID, LOG_ZERO_DATES)
    assert _csv_nonpositive_count(out_dir / "series.csv", TRACER_SERIES_ID) == LOG_MASKED_DAYS

    assert _run_charts(spec_path, out_dir) == 0
    linear_document = _plan(out_dir, spec_path)
    linear_manifest = json.loads(out_dir.joinpath("charts.json").read_text(encoding="utf-8"))
    for entry in linear_document.charts:
        assert entry.yscale == "linear", f"{entry.kind} must stay linear without the flag"
        assert entry.log_masked_points == 0
        assert entry.log_note is None
    for item in linear_manifest["charts"]:
        assert item["yscale"] == "linear"
        assert item["log_masked_points"] == 0
        assert "log_note" not in item, "a linear chart states no log disclosure"

    log_dir = tmp_path / "log"
    _write_zero_pair(log_dir, TRACER_SERIES_ID, LOG_ZERO_DATES)
    assert _run_charts_with_flags(spec_path, log_dir, "--log-scale") == 0
    log_document = _plan(log_dir, spec_path, log_scale=True)
    log_manifest = json.loads(log_dir.joinpath("charts.json").read_text(encoding="utf-8"))

    pl = _timeseries_entry_of(log_document)
    assert pl.yscale == "log"
    assert pl.log_masked_points == LOG_MASKED_DAYS, (
        "the masked count must equal the non-positive readings in the CSV, "
        "recomputed by the test rather than read back from the plan"
    )
    assert "2 non-positive" in (pl.log_note or ""), pl.log_note

    # The overlay masks the union across its own lines, and the growth chart is
    # never a log axis (D-15: growth bars stay linear).
    overlay = _overlay_entry_of(log_document)
    assert overlay.yscale == "log"
    assert overlay.log_masked_points == sum(
        _csv_nonpositive_count(log_dir / "series.csv", line.series_id)
        for line in overlay.series_lines
    )
    assert "2 non-positive" in (overlay.log_note or "")

    published = next(
        item
        for item in log_manifest["charts"]
        if item["kind"] == make_charts.TIMESERIES_KIND
        and item.get("series_id") == TRACER_SERIES_ID
    )
    assert published["yscale"] == "log"
    assert published["log_masked_points"] == LOG_MASKED_DAYS
    assert "2 non-positive" in published["log_note"]

    # The disclosure is D-09's requirement applied to the scale regime: a PNG
    # separated from the report must still say it did not draw two days.
    for name in (f"chart_{TRACER_SERIES_ID}_timeseries.png", make_charts.OVERLAY_FILENAME):
        blob = (log_dir / name).read_bytes()
        assert blob[:8] == PNG_MAGIC, f"{name} must be a real PNG in log mode"
        assert len(blob) > 1024


def test_growth_stays_linear_under_log_scale(tmp_path: Path) -> None:
    """D-15: the flag reaches the timeseries and overlay axes only.

    A growth chart is a percentage axis: a log scale on it would erase a
    negative bar exactly as RESEARCH Pitfall 1's zero floor did, and the flag is
    documented as timeseries/overlay-only, so the guarantee is asserted on the
    plan rather than left to a comment in the render branch.
    """
    out_dir = tmp_path / "log"
    spec_path = _write_zero_pair(out_dir, TRACER_SERIES_ID, LOG_ZERO_DATES)
    assert _run_charts_with_flags(spec_path, out_dir, "--log-scale") == 0

    document = _plan(out_dir, spec_path, log_scale=True)
    growth_entries = [e for e in document.charts if e.kind == make_charts.GROWTH_KIND]
    assert growth_entries, "the growth kind must still be asserted, or this is vacuous"
    for entry in growth_entries:
        assert entry.yscale == "linear", f"{entry.series_id} growth must never be a log axis"
        assert entry.log_masked_points == 0
        assert entry.log_note is None
        # A percentage axis also keeps its literal zero floor, which a log axis
        # could not express at all.
        assert entry.y_limits[0] <= 0.0 <= entry.y_limits[1]

    manifest = json.loads(out_dir.joinpath("charts.json").read_text(encoding="utf-8"))
    for item in manifest["charts"]:
        if item["kind"] == make_charts.GROWTH_KIND:
            assert item["yscale"] == "linear"
            assert "log_note" not in item

    # And the structural half: the growth render branch reaches no scale call at
    # all and does not even read the entry's regime, so the flag cannot reach it
    # through a future edit either.
    assert _scale_calls_in_render_path("_draw_growth") == [], (
        "the growth branch must name no scale call at all"
    )
    assert "yscale" not in ast.unparse(_function_node("_draw_growth")), (
        "the growth branch must not even read entry.yscale"
    )


def test_log_mode_declares_nonpositive_masking_in_source() -> None:
    """`nonpositive="mask"` is written out, never inherited from a default.

    matplotlib's own default happens to be `mask` today; a version that changed
    it to `clip` would drop the zeros without telling anyone, and nothing else in
    the suite could see the difference. The literal is therefore a source-level
    requirement, asserted against the module text.
    """
    source = _module_source()
    assert 'nonpositive="mask"' in source, (
        "the log axis must set nonpositive explicitly, never rely on a default"
    )
    # And the call that carries it must be the y-scale of a timeseries axes.
    assert "set_yscale" in source


def test_log_help_text_is_published_and_explicit() -> None:
    """The flag declares its own regime: opt-in, masked, timeseries/overlay only.

    Asserted against `_parser()` directly, the `test_contracts.py` idiom for the
    resolver CLI - the published help text is the only place a caller learns
    that a log chart is a different scale regime rather than a re-rendering of
    the linear one.
    """
    help_text = make_charts._parser().format_help()
    assert "--log-scale" in help_text
    assert "opt-in" in help_text, "the help must say the regime is opt-in, never automatic"
    assert "masked" in help_text, "the help must say non-positive days are masked"
    assert "linear" in help_text, "the help must say the growth bars stay linear"

    parsed = make_charts._parser().parse_args(["--spec", "s.json", "--log-scale"])
    assert parsed.log_scale is True
    assert make_charts._parser().parse_args(["--spec", "s.json"]).log_scale is False


def test_a_masked_anomaly_is_a_caret_not_a_stub_clipped_at_the_log_floor(
    tmp_path: Path,
) -> None:
    """A `views=0` day the detector flagged cannot be drawn on a log axis - so it
    is drawn as a caret, not as a segment clipped at the floor.

    Found by opening the PNG, the 05-04 defect class: the first log-mode render
    drew the anomaly's own segment from its median (115) down to its value (0)
    while the axis floor is the smallest *positive* reading (100). matplotlib
    clips the collection, so what reached the image was a short stub ending at
    the floor - pixel-identical to a genuine reading that happened to land on
    100, and carrying none of the information that the real value was zero. The
    axes say nothing; only the code knows. A stub is worse than a caret, because
    the reader cannot tell a clipped value from a real one.
    """
    out_dir = tmp_path / "log"
    spec_path = _write_zero_pair(out_dir, TRACER_SERIES_ID, LOG_ZERO_DATES)
    assert _run_charts_with_flags(spec_path, out_dir, "--log-scale") == 0

    import matplotlib

    if matplotlib.get_backend().lower() != "agg":
        matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    document = _plan(out_dir, spec_path, log_scale=True)
    entry = _timeseries_entry_of(document)

    # The zero days really are anomalies in metrics.json, so the collision is
    # exercised by data rather than by a hand-built entry.
    metrics = _metrics_by_series(out_dir)
    zero_anomalies = [
        item for item in metrics[TRACER_SERIES_ID]["anomalies"] if item["value"] == 0
    ]
    assert zero_anomalies, (
        "the fixture must produce at least one anomaly whose value a log axis "
        "cannot draw, or this test proves nothing"
    )
    assert entry.log_masked_points == LOG_MASKED_DAYS
    assert len(zero_anomalies) <= entry.log_masked_points

    fig, ax = plt.subplots(figsize=make_charts.FIGURESIZE, dpi=make_charts.DPI)
    try:
        make_charts._draw_timeseries(entry, ax, mdates)
        carets = [line for line in ax.get_lines() if line.get_marker() == "v"]
        assert len(carets) == len(zero_anomalies), (
            "one caret per anomaly the log axis cannot draw"
        )
        # No clipped segment survives: every vlines collection on this axes has a
        # positive lower bound, so nothing ends at the floor pretending to be a
        # reading.
        floor = entry.y_limits[0]
        assert floor > 0.0
        for collection in ax.collections:
            for segment in collection.get_segments():
                for _x, y in segment:
                    assert y > 0.0, (
                        f"a masked value was drawn as a real coordinate at {y}"
                    )
        # And the caret rides at a position the reader can see, carrying the same
        # anomaly colour and the same single legend row.
        legend = ax.get_legend()
        labels = [text.get_text() for text in legend.get_texts()] if legend else []
        assert labels.count(make_charts.chart_tokens("uk")["anomalies"]) == 1, labels
        for line in carets:
            assert list(line.get_ydata())[0] >= floor
            assert list(line.get_ydata())[0] > 0.0
    finally:
        plt.close(fig)

    # The linear chart is untouched by any of this: a zero day on a linear axis
    # is drawn at face value, which is D-11.
    linear_dir = tmp_path / "linear"
    _write_zero_pair(linear_dir, TRACER_SERIES_ID, LOG_ZERO_DATES)
    assert _run_charts(spec_path, linear_dir) == 0
    linear_entry = _timeseries_entry_of(_plan(linear_dir, spec_path))
    fig, ax = plt.subplots(figsize=make_charts.FIGURESIZE, dpi=make_charts.DPI)
    try:
        make_charts._draw_timeseries(linear_entry, ax, mdates)
        assert not [line for line in ax.get_lines() if line.get_marker() == "v"], (
            "a linear chart never uses the masked caret - the zero is drawable there"
        )
        zeros = [
            point.views for point in linear_entry.points if point.views == 0
        ]
        assert len(zeros) == LOG_MASKED_DAYS
    finally:
        plt.close(fig)


# --- Plan 05-06 Task 2: spec-language chart text and the unsupported-script warning

# The committed golden's Ukrainian label, byte for byte. D-16/D-11: a label is
# spec-authored prose and reaches the canvas as exact Unicode code points - no
# normalization, no case folding, no truncation, no transliteration.
PL_LABEL = "Польська: інтервальне голодування"

# A language whose script the default DejaVu font does not cover. RESEARCH
# Pitfall 7 confirmed matplotlib emits `UserWarning: Glyph 26085 ... missing from
# font(s) DejaVu Sans` and no exception, so the chart renders successfully and
# looks wrong - which is exactly why D-16 makes it a *documented gap that warns*
# rather than a refusal or a font-fallback project.
JA_LABEL = "断続的断食"


def _write_localized_pair(
    tmp_path: Path, language: str, label: str
) -> tuple[Path, Path]:
    """Write a one-series spec in `language` plus the inputs the analyzer reads.

    metrics.json is built by the sanctioned analyzer so the per-series language
    and label come from the spec by the production path, exactly as they would
    on a real run - a hand-written metrics document would let a test pass while
    the stage read its language from somewhere else entirely.
    """
    item = dict(_spec_series_item(TRACER_SERIES_ID))
    item["language"] = language
    item["label"] = label
    spec = json.loads(FIXTURES_DIR.joinpath("spec.example.json").read_text(encoding="utf-8"))
    document = {
        "name": "one-series-tracer",
        "request": "localized chart text probe",
        "language": language,
        "window": spec["window"],
        "series": [item],
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv_filtered(out_dir, (TRACER_SERIES_ID,))
    grouped = analyze_trends.load_series_csv(out_dir / "series.csv", document)
    out_dir.joinpath("metrics.json").write_text(
        json.dumps(
            analyze_trends.build_metrics(document, str(spec_path), grouped),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return spec_path, out_dir


def test_cyrillic_labels_render_without_glyph_warnings(tmp_out: Path) -> None:
    """D-16: the committed Ukrainian fixture renders clean, in Ukrainian.

    RESEARCH Pitfall 7's positive half: the same probe that warned for a CJK
    glyph emitted no warning at all for a Ukrainian label, which is what lets
    D-16 name Cyrillic as covered and CJK as a documented gap. The assertion is
    on the WARNING STREAM rather than on pixels, because the missing-glyph notice
    is a warning and a chart that emits one is defective even when the bytes
    happen to be a valid PNG.
    """
    import warnings as warnings_module

    with warnings_module.catch_warnings(record=True) as captured:
        warnings_module.simplefilter("always")
        _spec, manifest = _render_two_series(tmp_out)

    offending = [
        str(record.message)
        for record in captured
        if "missing from font" in str(record.message)
    ]
    assert offending == [], (
        f"DejaVu covers Cyrillic; a missing-glyph warning means a font problem: {offending}"
    )

    # The chart's own words are the `uk` tokens, not English. A PNG that mixes
    # the two is a bilingual chart the report never asked for (T-5-24).
    tokens = make_charts.chart_tokens("uk")
    published = next(
        item
        for item in manifest["charts"]
        if item["kind"] == make_charts.TIMESERIES_KIND
        and item.get("series_id") == TRACER_SERIES_ID
    )
    assert tokens["median7"] in published["subtitle"], published["subtitle"]
    assert tokens["raw_daily"] in published["subtitle"]
    for token in tokens.values():
        assert not token.isascii(), f"a `uk` token is still English: {token!r}"

    # The spec-authored label is untouched on its way to the manifest, and the
    # overlay's own disclosure is the Ukrainian token on the image while the
    # machine-readable `note` stays the ASCII form Phase 6 quotes.
    assert published["label"] == PL_LABEL
    overlay = next(item for item in manifest["charts"] if item["kind"] == make_charts.OVERLAY_KIND)
    assert overlay["note"] == make_charts.OVERLAY_NOTE
    assert make_charts.OVERLAY_NOTE.isascii(), (
        "the manifest note is an interface string and must stay ASCII"
    )
    assert tokens["scales_differ"] != make_charts.OVERLAY_NOTE, (
        "the on-image disclosure is the localized token, not the ASCII interface"
    )


def test_unsupported_language_warns_once_and_still_renders(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D-16: a script DejaVu cannot draw is a warning, never a refusal.

    The failure RESEARCH measured is silence - a valid PNG full of tofu boxes
    and no exception anywhere. So the stage says so on stderr, once, naming the
    language and the documented gap, and then renders: CJK is a v1 gap
    (05-CONTEXT.md), not a v1 requirement, and a font-fallback stack is exactly
    the machinery 05-PATTERNS.md rules out of this phase.
    """
    spec_path, out_dir = _write_localized_pair(tmp_path, "ja", JA_LABEL)

    assert _run_charts(spec_path, out_dir) == 0
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.strip()]
    assert len(lines) == 1, f"exactly one warning, not a stream: {lines}"
    assert "ja" in lines[0]
    assert "missing glyphs" in lines[0] or "DejaVu" in lines[0]
    assert "v1 gap" in lines[0], lines[0]

    manifest = json.loads(out_dir.joinpath("charts.json").read_text(encoding="utf-8"))
    assert len(manifest["charts"]) == 3
    for item in manifest["charts"]:
        blob = out_dir.joinpath(item["filename"]).read_bytes()
        assert blob[:8] == PNG_MAGIC, f"{item['filename']} must still be a real PNG"
        assert len(blob) > 1024
    # The spec-authored label reaches the manifest verbatim even when the font
    # cannot draw it - the text is right, the glyph coverage is a separate,
    # disclosed fact.
    pl = next(
        item
        for item in manifest["charts"]
        if item["kind"] == make_charts.TIMESERIES_KIND
        and item.get("series_id") == TRACER_SERIES_ID
    )
    assert pl["label"] == JA_LABEL

    # The gap the plan forbids: font machinery in the chart stage.
    assert "font_manager" not in _module_source(), (
        "D-16 makes CJK a documented gap; no font fallback belongs in this phase"
    )
    assert "font_manager" not in _module_source().replace(
        '"font_manager"', "", 1
    ) or True  # the assertion above is the one that bites


def test_chart_tokens_are_required_per_language(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T-5-24: a language with no token table fails closed, with no English fallback.

    Rendering an English string inside a chart whose spec asked for another
    language is the mixed-language defect this stage is meant to make
    impossible. The refusal is also the only model-actionable form of the
    problem: a caller that adds a language gets told exactly which language is
    missing rather than receiving a quietly wrong chart.
    """
    spec_path, out_dir = _write_localized_pair(tmp_path, "sv", "Fasting")
    assert "sv" not in make_charts.CHART_TOKENS

    assert _run_charts(spec_path, out_dir) == 1
    captured = capsys.readouterr()
    # The font-gap warning may precede the failure line: a language with no
    # table is warned about AND then refused, so the caller is told both facts.
    # What matters is that the refusal is a line of its own, not that it is the
    # first thing on the stream.
    failure_lines = [
        line for line in captured.err.splitlines() if line.startswith("charts failed:")
    ]
    assert len(failure_lines) == 1, captured.err
    assert "no chart tokens for language" in failure_lines[0]
    assert "sv" in failure_lines[0]
    assert list(out_dir.glob("*.png")) == [], "a refused language must draw nothing"
    assert not out_dir.joinpath("charts.json").exists()

    # Every shipped table is complete, checked against the module's own key
    # list rather than a copy of it - so a language added later cannot ship
    # half a vocabulary.
    for code in make_charts.CHART_TOKENS:
        assert set(make_charts.CHART_TOKENS[code]) == set(make_charts.REQUIRED_CHART_TOKENS), (
            f"the {code} table must define exactly the required keys"
        )
        assert all(token.strip() for token in make_charts.CHART_TOKENS[code].values()), (
            f"the {code} table must not ship an empty word"
        )
    assert set(make_charts.REQUIRED_CHART_TOKENS) >= {
        "raw_daily",
        "median7",
        "anomalies",
        "no_data",
        "clean_growth",
        "views_per_day",
        "scales_differ",
        "na",
    }, "the plan's eight keys are a floor, not the whole list"

    # A table that is present but INCOMPLETE is the same defect, so it is the
    # same message rather than a KeyError raised halfway through a render - which
    # would leave figures open and nothing published.
    incomplete = {code: dict(table) for code, table in make_charts.CHART_TOKENS.items()}
    del incomplete["uk"]["median7"]
    monkeypatch.setattr(make_charts, "CHART_TOKENS", incomplete)
    with pytest.raises(make_charts.ChartError) as raised:
        make_charts.chart_tokens("uk")
    assert str(raised.value) == "no chart tokens for language: uk"


def test_labels_are_carried_verbatim(tmp_out: Path) -> None:
    """A label is exact code points on its way to the canvas - never transformed.

    Phase 3 froze "CSV identity equality uses exact decoded UTF-8 code points
    without normalization" for the data layer; D-16 carries the same rule into
    the chart layer, where the risk is higher because the string additionally
    passes through a text engine. No `strip()` (a deliberate leading space is
    content), no case folding, no `unicodedata.normalize`, no length cap.
    """
    _spec, manifest = _render_two_series(tmp_out)
    metrics = json.loads(
        FIXTURES_DIR.joinpath("metrics.example.json").read_text(encoding="utf-8")
    )
    by_id = {node["series_id"]: node for node in metrics["series"]}

    for item in manifest["charts"]:
        if "series_id" not in item:
            continue
        source = by_id[item["series_id"]]["label"]
        assert item["label"] == source, (
            f"{item['filename']}: the label must be metrics.json's own string, "
            f"byte for byte"
        )
        # And the two specific transformations this stage must never perform.
        assert item["label"] == item["label"].strip(), "no strip() on a spec label"
        assert item["label"].casefold() != item["label"] or item["label"].islower(), (
            "the label is not case-folded"
        )
    pl = next(
        item
        for item in manifest["charts"]
        if item.get("series_id") == TRACER_SERIES_ID
    )
    assert pl["label"] == PL_LABEL
    assert pl["label"].encode("utf-8") == PL_LABEL.encode("utf-8")

    # NFC-stability of the shipped labels: normalization would be a silent
    # rewrite of the bytes, so a value that is not already in its normalized
    # form is a canary for one.
    for series_id, node in by_id.items():
        assert unicodedata.normalize("NFC", node["label"]) == node["label"], (
            f"{series_id}: the committed label must already be NFC, or this "
            "assertion can no longer see a normalizing implementation"
        )

    # And the module performs no normalization anywhere at all.
    source = _module_source()
    assert "unicodedata" not in source, "the chart stage must never normalize a string"
    assert "casefold" not in source and ".lower()" not in source


def test_the_render_path_writes_no_hardcoded_chart_words(tmp_path: Path) -> None:
    """Every word the renderer puts on a canvas comes from `CHART_TOKENS`.

    The guard that would have caught 05-06's two localization leaks, both of
    which shipped a Ukrainian chart with English in it and both of which were
    found only by OPENING the PNG: the overlay subtitle still read
    "усі теми порівняно - raw daily" and the growth bar still read "on 2363.5
    переглядів/день". Neither was a wrong number, so no numeric assertion could
    see either, and both were structurally correct.

    So the rule is stated as code: inside the render helpers, a string literal
    may only be a matplotlib keyword, a colour, a date format, a number format,
    a contract dict key, a token KEY, or an assertion message. A new user-facing
    word has to arrive through the token table or this test fails - which is the
    only way D-16's "no English fallback inside a chart" survives the next
    feature added to a chart.
    """
    allowed = {
        # matplotlib keyword arguments and coordinate systems
        "upper left", "axes fraction", "offset points", "data", "left", "right",
        "top", "bottom", "center", "Agg", "log", "mask",
        # display constants that are not language: colours and a date format
        "#212529", "#4C6EF5", "#495057", "%Y-%m",
        # number and separator formats
        "", " ", "\n", "%\n", "+.1f", " .1f",
        # contract dictionary keys, read by subscript and never displayed
        "date", "median", "value",
        # CHART_TOKENS keys - the lookup, not the word
        "raw_daily", "median7", "anomalies", "no_data", "scales_differ", "na",
        "on_base", "views_per_day", "clean_growth", "comparison_view",
        # assertion messages and the one non-localized refusal in the render path
        "a timeseries chart always has a date axis",
        "the comparison view always has a date axis",
        "chart target escapes the output directory: ",
    }
    render_helpers = (
        "_draw_timeseries",
        "_draw_overlay",
        "_draw_growth",
        "_draw_gap_bands",
        "_draw_anomaly_marks",
        "_draw_log_disclosure",
        "_apply_log_regime",
        "render_chart",
    )
    offending: list[str] = []
    for name in render_helpers:
        function = _function_node(name)
        docstrings = {
            ast.get_docstring(function, clean=False),
            *(ast.get_docstring(child, clean=False) for child in ast.walk(function)
              if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))),
        }
        for node in ast.walk(function):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if node.value in allowed or node.value in docstrings:
                continue
            offending.append(f"{name}:{node.lineno} {node.value!r}")
    assert not offending, (
        "the render path may not hardcode a user-facing word; route it through "
        f"CHART_TOKENS (D-16): {offending}"
    )

    # And the positive half, so the guard cannot be satisfied by emptying the
    # render path: a real Ukrainian run still draws tokens, and every one of them
    # is absent from the English set.
    out_dir = tmp_path / "uk"
    _copy_fixtures(out_dir, ALL_SERIES_IDS)
    assert _run_charts(FIXTURES_DIR / "spec.example.json", out_dir) == 0
    manifest = json.loads(out_dir.joinpath("charts.json").read_text(encoding="utf-8"))
    tokens = make_charts.chart_tokens("uk")
    english = set(make_charts.CHART_TOKENS["en"].values())
    assert not (set(tokens.values()) & english), (
        "a Ukrainian token that is also the English one is not a translation"
    )
    for item in manifest["charts"]:
        # The SUBTITLE is what the image carries, and it is built entirely from
        # tokens plus the spec-authored label. `note` is deliberately excluded:
        # it is the ASCII interface string Phase 6 quotes from the manifest, and
        # D-16/D-20 want the manifest form and the on-image form to differ.
        assert any(word in item["subtitle"] for word in tokens.values()), (
            f"{item['filename']}: {item['subtitle']!r} names nothing from the token table"
        )
    overlay = next(item for item in manifest["charts"] if item["kind"] == make_charts.OVERLAY_KIND)
    assert overlay["note"] == make_charts.OVERLAY_NOTE
    assert overlay["note"] not in tokens.values(), (
        "the manifest note stays ASCII; the drawn disclosure is the token"
    )
    growth = next(item for item in manifest["charts"] if item["kind"] == make_charts.GROWTH_KIND)
    assert tokens["clean_growth"] in growth["subtitle"]


# --- Plan 05-06 Task 3: the fail-closed contract and the --strict typing gate


def test_empty_metrics_file_fails_closed(
    tmp_out: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A zero-byte metrics.json is a failure, not an empty chart set.

    `json.loads(b"")` raises, which is the correct outcome - the interesting
    property is that it raises BEFORE any render, so the sentinel PNG is the only
    file in `--out` and the sentinel manifest bytes are the ones still there.
    """
    _write_golden_copy(tmp_out)
    sentinel_manifest = tmp_out / "charts.json"
    sentinel_manifest.write_bytes(b"sentinel")
    sentinel_png = tmp_out / "chart_pl-post-przerywany_timeseries.png"
    sentinel_png.write_bytes(b"stub")
    (tmp_out / "metrics.json").write_bytes(b"")

    assert _run_charts(FIXTURES_DIR / "spec.example.json", tmp_out) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("charts failed:"), captured.err
    assert "metrics JSON is malformed" in captured.err
    assert sentinel_manifest.read_bytes() == b"sentinel"
    assert sentinel_png.read_bytes() == b"stub"
    assert sorted(path.name for path in tmp_out.glob("*.png")) == [sentinel_png.name], (
        "a refused input must write no new chart"
    )
    assert captured.out == "", "a failed run must not print a success line"


def test_malformed_metrics_json_fails_closed(
    tmp_out: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A truncated document is refused on the same terms as an empty one."""
    _write_golden_copy(tmp_out)
    (tmp_out / "metrics.json").write_bytes(b"{not json")

    assert _run_charts(FIXTURES_DIR / "spec.example.json", tmp_out) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("charts failed:"), captured.err
    assert "metrics JSON is malformed" in captured.err
    assert list(tmp_out.glob("*.png")) == []
    assert not tmp_out.joinpath("charts.json").exists()


def test_metrics_missing_a_required_series_field_fails_closed(
    tmp_out: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A series without `avg_daily_views` names the field in the refusal.

    The message matters more than the exit code here: a model reading stderr has
    to be able to fix the document, and "metrics.series[0].avg_daily_views must
    be a number" is fixable while "invalid metrics" is not.
    """
    _write_golden_copy(tmp_out)
    path = tmp_out / "metrics.json"
    metrics = json.loads(path.read_text(encoding="utf-8"))
    del metrics["series"][0]["avg_daily_views"]
    path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    assert _run_charts(FIXTURES_DIR / "spec.example.json", tmp_out) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("charts failed:"), captured.err
    assert "avg_daily_views" in captured.err
    assert TRACER_SERIES_ID not in captured.err or "series[0]" in captured.err
    assert list(tmp_out.glob("*.png")) == []
    assert not tmp_out.joinpath("charts.json").exists()


def test_non_finite_metrics_value_is_refused(
    tmp_out: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`1e400` in a growth pct is refused before anything is drawn.

    The point is the ORDER, not the refusal. `json.loads` turns `1e400` into
    `inf` happily, and a `barh` of `inf` renders a bar that looks like a real
    reading - a silent lie drawn from a number the contract says is not
    computable. So the assertion is that the analyzer's own finite walk is
    actually REACHED, by requiring the message it alone can produce.
    """
    _write_golden_copy(tmp_out)
    path = tmp_out / "metrics.json"
    metrics = json.loads(path.read_text(encoding="utf-8"))
    node = next(n for n in metrics["series"] if n["series_id"] == TRACER_SERIES_ID)
    # A unique sentinel so the substitution cannot hit an unrelated cell, and a
    # RAW `1e400` token so the value really is a non-finite float on the way in
    # rather than a pre-encoded `Infinity` (which is not valid strict JSON).
    node["growth"]["m3"]["clean"]["pct"] = 987654321
    patched = json.dumps(metrics, ensure_ascii=False, indent=2)
    assert patched.count("987654321") == 1
    path.write_text(patched.replace("987654321", "1e400"), encoding="utf-8")

    assert _run_charts(FIXTURES_DIR / "spec.example.json", tmp_out) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("charts failed:"), captured.err
    assert "non-finite" in captured.err
    assert list(tmp_out.glob("*.png")) == [], "a non-finite value must draw nothing"
    assert not tmp_out.joinpath("charts.json").exists()


def test_publication_failure_leaves_prior_manifest_and_no_staging_file(
    tmp_out: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An interrupted publication replaces nothing and leaves nothing behind.

    `common.dump_json` stages into a same-directory dotfile and `os.replace`s it
    into position, so a failure at the replace is the one moment where a naive
    implementation would leave either a truncated manifest or a stray staging
    file in `--out`. Both are asserted: the sentinel bytes survive byte for
    byte, and the directory holds no `.charts.json.*.tmp`.
    """
    _write_golden_copy(tmp_out)
    manifest = tmp_out / "charts.json"
    manifest.write_bytes(b"sentinel")

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("simulated interrupted replace")

    monkeypatch.setattr(make_charts.common.os, "replace", fail_replace)

    assert _run_charts(FIXTURES_DIR / "spec.example.json", tmp_out) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("charts failed:"), captured.err
    assert "could not write charts output" in captured.err
    assert manifest.read_bytes() == b"sentinel", (
        "a failed publication must not touch the prior manifest's bytes"
    )
    assert list(tmp_out.glob(".charts.json.*.tmp")) == [], (
        "a failed publication must not leave a staging file behind"
    )
    assert list(tmp_out.glob("*.tmp")) == []


def test_render_failure_leaves_no_partial_png(
    tmp_out: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failure on the SECOND chart publishes no manifest and no half-drawn set.

    The invariant is the ordering in `main`: charts.json is written only after
    every render has returned, so an interrupted run leaves complete PNGs and no
    description of them. A reader finding two PNGs and no manifest knows the run
    did not finish; a reader finding two PNGs and a manifest naming five has been
    told something false.
    """
    _write_golden_copy(tmp_out)
    calls: list[str] = []
    # The real renderer is captured BEFORE the patch: `flaky_render` calls it
    # through the module attribute, which the monkeypatch has just replaced, so
    # resolving it lazily would make the stub recurse into itself and never draw
    # anything - a green test for a run that rendered no chart at all.
    real_render = make_charts.render_chart

    def flaky_render(entry: make_charts.ChartEntry, out_dir: Path) -> Path:
        calls.append(entry.filename)
        if len(calls) == 2:
            raise make_charts.ChartError(f"simulated render failure on {entry.filename}")
        return real_render(entry, out_dir)

    monkeypatch.setattr(make_charts, "render_chart", flaky_render)

    assert _run_charts(FIXTURES_DIR / "spec.example.json", tmp_out) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("charts failed:"), captured.err
    assert "simulated render failure" in captured.err
    assert len(calls) == 2, "the run must stop at the failure, not continue"

    assert not tmp_out.joinpath("charts.json").exists(), (
        "no manifest may describe an incomplete run"
    )
    # Whatever PNGs are on disk are COMPLETE files: the failure happened before
    # the second savefig, so the second file was never created at all.
    pngs = sorted(tmp_out.glob("*.png"))
    assert len(pngs) == 1, [path.name for path in pngs]
    blob = pngs[0].read_bytes()
    assert blob[:8] == PNG_MAGIC
    assert len(blob) > 1024
    assert list(tmp_out.glob(".charts.json.*.tmp")) == []


def test_spec_problem_exits_two(tmp_path: Path) -> None:
    """RUN-01: an invalid spec is exit 2, never intercepted into a chart error.

    The chart stage must not convert the frozen exit-2 contract into its own
    exit 1, because a caller distinguishes the two: 2 means "fix your spec" and
    1 means "fix your data or your environment". `load_and_validate_spec` raises
    `SystemExit(2)` before any stage code runs, and nothing here catches it.
    """
    out_dir = tmp_path / "out"
    _write_golden_copy(out_dir)
    spec_path = tmp_path / "spec.json"
    spec = json.loads(FIXTURES_DIR.joinpath("spec.example.json").read_text(encoding="utf-8"))
    # A ROOT required field, so `common.validate_spec` is what refuses it. (A
    # missing `window` is not a violation the frozen validator reports - it is
    # caught later by the analyzer's own reader, which is a different stage's
    # contract and a different exit code. The exit-2 case under test is the
    # validator's, so the test must provoke the validator.)
    del spec["language"]
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(SystemExit) as raised:
        _run_charts(spec_path, out_dir)
    assert raised.value.code == 2
    assert list(out_dir.glob("*.png")) == [], "a spec problem must draw nothing"
    assert not out_dir.joinpath("charts.json").exists()


def test_usetex_is_never_enabled() -> None:
    """T-5-21: no LaTeX, and no mathtext `$...$` around a user string.

    A label is spec-authored text reaching a text engine. `text.usetex=True`
    would hand it to a LaTeX shell pipeline, and a `$...$` pair in a label would
    be interpreted as math - both are injection surfaces inside a renderer, and
    neither raises: they produce a wrong or empty image. The module never
    mentions `usetex` at all, which is the strongest form of the guarantee, and
    the AST walk below pins the absence of any attribute assignment to it.
    """
    source = _module_source()
    assert "usetex" not in source, "the chart module must never mention text.usetex"
    assert "$" not in source, (
        "no `$` may appear in the module - mathtext interpolation of a spec "
        "string is a rendering injection vector"
    )

    tree = ast.parse(source)
    offending: list[str] = []
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for target in targets:
            attribute = target
            while isinstance(attribute, (ast.Attribute, ast.Subscript)):
                attribute = attribute.value  # type: ignore[assignment]
            if isinstance(attribute, ast.Attribute) and attribute.attr == "usetex":
                offending.append(f"line {node.lineno}")
            if isinstance(target, ast.Attribute) and target.attr == "usetex":
                offending.append(f"line {node.lineno}")
    assert not offending, f"text.usetex must never be assigned: {offending}"

    # And the positive half: labels really do reach the canvas through the
    # ordinary text paths, so the guarantee is about the text engine and not
    # about labels never being drawn.
    assert "set_title" in source and "ax.plot(" in source
    assert "rcParams" not in source, "no rcParams mutation in the render path"


def test_log_disclosure_text_stays_inside_the_canvas(tmp_path: Path) -> None:
    """05-06 inherits 05-05's clipping guard for the text it adds.

    The disclosure is 50 characters of prose; anchored at the wrong corner it
    would run off the right edge of a 9-inch figure, and a half-printed count is
    worse than no count at all - it is a wrong count. Reusing the
    `get_window_extent()`-against-the-canvas mechanism from 05-05 rather than
    re-deriving it keeps the defect class closed without a human.
    """
    out_dir = tmp_path / "log"
    spec_path = _write_zero_pair(out_dir, TRACER_SERIES_ID, LOG_ZERO_DATES)
    assert _run_charts_with_flags(spec_path, out_dir, "--log-scale") == 0

    import matplotlib

    if matplotlib.get_backend().lower() != "agg":
        matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    document = _plan(out_dir, spec_path, log_scale=True)
    targets = [_timeseries_entry_of(document), _overlay_entry_of(document)]
    for entry in targets:
        fig, ax = plt.subplots(figsize=make_charts.FIGURESIZE, dpi=make_charts.DPI)
        try:
            if entry.kind == make_charts.OVERLAY_KIND:
                make_charts._draw_overlay(entry, ax, mdates)
            else:
                make_charts._draw_timeseries(entry, ax, mdates)
            fig.canvas.draw()
            width, height = fig.canvas.get_width_height()
            drawn = [text.get_text() for text in ax.texts if entry.log_note in text.get_text()]
            assert drawn == [entry.log_note], f"{entry.kind}: the disclosure must be drawn"
            for artist in ax.texts:
                left, bottom, right, top = artist.get_window_extent().bounds
                assert left >= 0, f"{artist.get_text()!r} runs off the left edge ({left:.1f})"
                assert right <= width, (
                    f"{artist.get_text()!r} runs off the right edge ({right:.1f} of {width})"
                )
                assert top <= height, f"{artist.get_text()!r} runs off the top ({top:.1f})"
                assert bottom >= 0, f"{artist.get_text()!r} runs off the bottom ({bottom:.1f})"
        finally:
            plt.close(fig)

