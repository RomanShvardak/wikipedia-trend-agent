"""Offline tests for the Wikipedia trend report stage.

The guarantee this module states: the report stage is offline, stdlib-only, and
writes **only** into the conftest `tmp_out` directory. It never reaches the
network, never imports a third-party package, and never mutates a committed
fixture — the last of those is asserted after every test and again at the end
of the module-scoped fixture's life, so a renderer that wrote back into
`tests/fixtures/` would fail here rather than silently corrupting the golden
documents every other phase's tests read.

The three local inputs a real run needs are produced by running the REAL
`make_charts` stage into a temporary directory rather than by committing a
hand-written `charts.json`: a hand-written sibling manifest could drift from the
chart emitter, and then the report would be passing against a document the
pipeline never writes. This module is the tracer for plan 06-01 — it proves the
whole document on the thinnest path, not the fidelity edges, which 06-02 and
06-03 land.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest

import build_report
import make_charts


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# The fixtures this stage reads or copies. Their bytes are compared before and
# after every test, because a stage that rewrites its own golden input is a
# stage whose "passing" tests prove nothing on the second run.
COMMITTED_FIXTURES = (
    "metrics.example.json",
    "metrics.anomalies.example.json",
    "series.example.csv",
    "spec.example.json",
)

# A Markdown image reference, captured as a whole. Non-greedy on both ends so a
# label carrying a bracket cannot run the match into the next image.
IMAGE_REF = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")

# The report stage's own import and call surface. `os` and `tempfile` are
# deliberately ABSENT: atomic publication is `common.dump_json`'s mechanism and
# the plan inherits it, so forbidding them here would forbid the contract. What
# is forbidden is a third-party package, a shell, or a locale-aware formatter.
FORBIDDEN_IMPORTS = frozenset(
    {
        "matplotlib",
        "numpy",
        "pandas",
        "seaborn",
        "plotly",
        "altair",
        "statsmodels",
        "subprocess",
        "requests",
        "httpx",
        # The offline claim, enforced rather than asserted in prose. The plan
        # enumerated a third-party + shell + locale blacklist, and the stdlib
        # route to a connection was missing from it: `import urllib.request` in
        # a report stage would have passed every test in this module while
        # making COVERAGE.md's "opens no connection" false. `test_analyze.py`
        # already asserts `"urllib" not in source` for the same reason; as an
        # AST rule this also covers `from http.client import ...`, which a
        # substring check would miss. The AST test splits every dotted name, so
        # `http.client` and `urllib.request` reduce to the two keys below.
        "urllib",
        "http",
        "socket",
        "weasyprint",
        "jinja2",
        "markdown",
        "tabulate",
        # CONTRACTS.md 8.4: PEP 378's specs are not locale-aware on purpose, so
        # a `locale` import would silently change the digits a reader sees.
        "locale",
        "pickle",
        "multiprocessing",
        "runpy",
    }
)
FORBIDDEN_CALLS = frozenset(
    {"eval", "exec", "compile", "__import__", "system", "popen", "run"}
)

# The ten ratified report.v1 keys, restated here so the tracer is readable on
# its own. `test_contracts.REPORT_V1_TOP_LEVEL` is the binding copy, and 06-03
# adds the field-drift test that asserts the two agree AND that the real emitter
# matches — a tracer that imported the constant could not tell a drifted emitter
# from a drifted constant.
RATIFIED_TOP_LEVEL = frozenset(
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


def _fixture_bytes() -> dict[str, bytes]:
    """Every committed fixture's exact bytes, for the immutability comparison."""
    return {name: (FIXTURES_DIR / name).read_bytes() for name in COMMITTED_FIXTURES}


@pytest.fixture(scope="module")
def metrics() -> Any:
    """The golden metrics document, with the fixture-immutability check at teardown.

    The teardown half is the point of making this a yield fixture: a per-test
    check catches a test that mutates a fixture, and a teardown check catches a
    module-level path that ran outside any single test's window.
    """
    before = _fixture_bytes()
    document = json.loads((FIXTURES_DIR / "metrics.example.json").read_text(encoding="utf-8"))
    yield document
    assert _fixture_bytes() == before, "a report test mutated a committed fixture"


# The chart stage reads FIXED input names in the output directory, so the
# committed fixtures are mapped onto them rather than copied under their own
# names. A test that "just worked" with `series.example.csv` in place would be
# proving nothing: the stage would have failed on a missing `series.csv`.
CHART_INPUTS = (
    ("series.example.csv", "series.csv"),
    ("metrics.example.json", "metrics.json"),
)


def _copy_chart_inputs(out_dir: Path) -> None:
    """Put the committed chart-stage inputs into `out_dir`, byte for byte."""
    for source, target in CHART_INPUTS:
        out_dir.joinpath(target).write_bytes((FIXTURES_DIR / source).read_bytes())


def _render_charts(tmp_out: Path) -> dict[str, Any]:
    """Run the REAL chart stage and return its parsed `charts.json`."""
    _copy_chart_inputs(tmp_out)
    spec_path = FIXTURES_DIR / "spec.example.json"
    assert make_charts.main(["--spec", str(spec_path), "--out", str(tmp_out)]) == 0
    return json.loads(tmp_out.joinpath("charts.json").read_text(encoding="utf-8"))


def _run_report(tmp_out: Path, spec_path: Path | None = None) -> int:
    """Run the real report stage over `tmp_out`, returning its exit code."""
    return build_report.main(
        [
            "--spec",
            str(spec_path if spec_path is not None else FIXTURES_DIR / "spec.example.json"),
            "--out",
            str(tmp_out),
        ]
    )


def _module_source() -> str:
    """Read the report module's own source, path resolved once here."""
    return Path(str(build_report.__file__)).read_text(encoding="utf-8")


def _section_order(text: str, language: str) -> list[int]:
    """The six frozen headings' indices in the rendered text, as the contract reads them.

    `index()` and a strict increase, never a set comparison: a document whose
    sections were all present but in the wrong order is the failure CONTRACTS.md
    §8.2 exists to make impossible, and a set check passes it.
    """
    tokens = build_report.report_tokens(language)
    return [
        text.index(tokens[key])
        for key in build_report.SECTION_TOKEN_KEYS
    ]


def _one_series_spec(tmp_path: Path, *, assumptions: list[str] | None) -> tuple[Path, Path]:
    """Derive a N=1 spec (and its N=1 metrics) from the committed two-series fixture.

    Derived through the same load-and-revalidate path the fail-closed tests use,
    never by hand-editing a committed fixture: a hand-built spec could carry a
    field the real validator rejects, and then the test would be measuring the
    builder rather than the edge. Exactly one `series` entry is kept and every
    other field keeps the fixture's own value.
    """
    import common

    source = json.loads((FIXTURES_DIR / "spec.example.json").read_text(encoding="utf-8"))
    source["series"] = [source["series"][0]]
    if assumptions is not None:
        source["assumptions"] = assumptions
    # Re-validate rather than assume: a derived spec the pipeline would refuse is
    # a broken test, and `common.validate_spec` is the pipeline's own verdict.
    assert common.validate_spec(source) == [], "the derived N=1 spec must be valid"

    spec_path = tmp_path / "spec.one.json"
    spec_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")

    source_metrics = json.loads(
        (FIXTURES_DIR / "metrics.example.json").read_text(encoding="utf-8")
    )
    source_metrics["series"] = [source_metrics["series"][0]]
    metrics_path = tmp_path / "metrics.one.json"
    metrics_path.write_text(json.dumps(source_metrics, ensure_ascii=False), encoding="utf-8")
    return spec_path, metrics_path


def _write_one_series_csv(out_dir: Path, series_id: str) -> None:
    """Derive a N=1 `series.csv` from the committed two-series fixture.

    Necessary, not cosmetic: `analyze_trends.load_series_csv` refuses a row whose
    `series_id` the spec does not declare, so a one-series spec paired with the
    unmodified two-series CSV fails at the CHART stage, before the report stage is
    ever reached. Filtering here keeps the derived input consistent with the
    derived spec, and it reads the committed fixture rather than editing it.
    """
    lines = (
        (FIXTURES_DIR / "series.example.csv").read_text(encoding="utf-8").splitlines()
    )
    header, *rows = lines
    assert header == "date,views,series_id,project,article", (
        f"the committed CSV header changed, so this filter is no longer column-accurate: {header!r}"
    )
    kept = [row for row in rows if row.split(",")[2:3] == [series_id]]
    assert kept, f"the derived N=1 CSV has no rows for {series_id}"
    out_dir.joinpath("series.csv").write_text(
        "\n".join([header, *kept]) + "\n", encoding="utf-8", newline="\n"
    )


def test_tracer_publishes_every_section_and_the_frozen_manifest(tmp_out: Path) -> None:
    """The whole report.v1 surface, rendered from the committed two-series fixture."""
    before = _fixture_bytes()
    charts = _render_charts(tmp_out)

    assert _run_report(tmp_out) == 0

    report_path = tmp_out / build_report.REPORT_FILENAME
    manifest_path = tmp_out / build_report.REPORT_MANIFEST_FILENAME
    assert report_path.is_file(), "report.md must be published"
    assert manifest_path.is_file(), "report.manifest.json must be published"

    text = report_path.read_text(encoding="utf-8")

    # Behaviour 2 - the six sections, strictly increasing by index().
    language = "uk"
    order = _section_order(text, language)
    assert order == sorted(order), f"the six sections are out of order: {order}"
    assert len(set(order)) == len(order), "a section heading appears more than once"

    # Behaviour 3 - exactly the ratified ten keys, nothing more, nothing less.
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(manifest) == RATIFIED_TOP_LEVEL, (
        f"the manifest drifted from the frozen surface: {sorted(manifest)}"
    )
    assert manifest["contract_version"] == "report.v1"
    assert manifest["report_filename"] == "report.md"
    assert manifest["formats"] == [{"format": "markdown", "filename": "report.md"}]
    assert manifest["language"] == language
    assert manifest["spec_name"] == charts["spec_name"]
    assert manifest["as_of"] == charts["as_of"]

    # Behaviour 4 - both digests come from the bytes on disk, and the report's
    # metrics digest is byte-identical to the chart manifest's.
    import hashlib

    metrics_on_disk = (tmp_out / "metrics.json").read_bytes()
    charts_on_disk = (tmp_out / "charts.json").read_bytes()
    assert manifest["metrics_sha256"] == hashlib.sha256(metrics_on_disk).hexdigest()
    assert manifest["charts_sha256"] == hashlib.sha256(charts_on_disk).hexdigest()
    assert manifest["metrics_sha256"] == charts["metrics_sha256"]

    # Behaviour 5 - the 2N+1 inventory, in published order, overlay last, every
    # name naming a real file.
    images = IMAGE_REF.findall(text)
    published = [entry["filename"] for entry in charts["charts"]]
    assert len(published) == 5, "the two-series fixture publishes 2N+1 = 5 charts"
    assert images == published, f"image order drifted from charts.json: {images}"
    assert images[-1] == make_charts.OVERLAY_FILENAME, "the overlay must be last"
    for name in images:
        assert (tmp_out / name).is_file(), f"report.md references a missing PNG: {name}"

    # The never-recompute rule, observable in the output: a null window is never
    # a zero and never an em-dash, and the raw pct never reaches the page.
    assert "0.0%" not in text, "a null clean.pct must not render as 0.0%"
    assert make_charts.OVERLAY_NOTE in text, "the overlay note is quoted verbatim"

    # Anomaly share is a bounded [0, 1] quantity, not a signed growth rate. It
    # once rendered through the percent seam and came out as "+0.0", whose sign
    # is a claim the number does not make. The fixture's clean percentages are
    # 3.5 / 16.7 / 5.7 / 30.8, so a signed zero can only be the share.
    assert "+0.0" not in text, "anomaly_share must not render with a signed percent spec"

    # The chart-stage disclosure lines must be pairwise DISTINCT. A series'
    # `timeseries` and `growth` entries share one `label`, so keying the line on
    # the label alone printed the same sentence twice and a reader could not tell
    # which of the two charts it described. One line per published entry, each
    # naming its own filename, is the property.
    disclosures = [line for line in text.splitlines() if "anomalies_drawn=" in line]
    assert len(disclosures) == len(published), (
        f"one disclosure per published chart, got {len(disclosures)} for {len(published)}"
    )
    assert len(set(disclosures)) == len(disclosures), (
        f"two charts share one disclosure line: {sorted(set(disclosures))}"
    )
    for entry in charts["charts"]:
        assert any(entry["filename"] in line for line in disclosures), (
            f"no disclosure line names {entry['filename']}"
        )

    assert _fixture_bytes() == before, "the run mutated a committed fixture"


def test_tracer_spec_problem_exits_two_and_writes_nothing(tmp_out: Path) -> None:
    """An invalid spec is exit 2 with neither output file written (behaviour 6)."""
    bad_spec = tmp_out / "spec.bad.json"
    bad_spec.write_text(json.dumps({"name": "x"}), encoding="utf-8")

    with pytest.raises(SystemExit) as exit_info:
        build_report.main(["--spec", str(bad_spec), "--out", str(tmp_out)])

    assert exit_info.value.code == 2
    assert not (tmp_out / build_report.REPORT_FILENAME).exists()
    assert not (tmp_out / build_report.REPORT_MANIFEST_FILENAME).exists()


def test_report_module_imports_no_third_party_dependency() -> None:
    """The report module's whole import and call surface, walked by AST."""
    tree = ast.parse(_module_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in FORBIDDEN_CALLS, (
                f"build_report must not call {node.func.id}()"
            )
        if isinstance(node, ast.Import):
            imported = {alias.name.split(".")[0] for alias in node.names}
            assert not imported & FORBIDDEN_IMPORTS, (
                f"build_report must not import {sorted(imported & FORBIDDEN_IMPORTS)}"
            )
        if isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[0]
            assert module not in FORBIDDEN_IMPORTS, (
                f"build_report must not import from {module}"
            )


def test_uk_report_tokens_carry_the_exact_roadmap_heading_strings() -> None:
    """The six `uk` headings are literals, not merely the order the tokens imply.

    Every other heading assertion in this module reads the strings back out of
    `REPORT_TOKENS`, so a mistyped Ukrainian heading satisfies all of them: the
    document renders the wrong word and the order is still strictly increasing.
    An injected-defect probe made that concrete — rewording
    `REPORT_TOKENS["uk"]["trust"]` to a different Ukrainian phrase left all four
    tests green. A gate never shown to fail has not been shown to pass, and a
    frozen contract value with no literal behind it is a value nothing defends.

    Four of the six are the exact strings ROADMAP SC#1 names. The other two —
    `Метрики` and `Графіки` — are the two SC#1 describes rather than names, fixed
    by this plan's own must_have and frozen in CONTRACTS.md §8.2, so they are
    pinned here on the same authority.
    """
    uk = build_report.REPORT_TOKENS["uk"]
    for key, expected in (
        ("conclusion", "Висновок"),
        ("metrics", "Метрики"),
        ("trust", "Наскільки можна довіряти"),
        ("charts", "Графіки"),
        ("limitations", "Обмеження та припущення"),
        ("next_step", "Наступний крок"),
    ):
        assert uk[key] == expected, (
            f"REPORT_TOKENS['uk'][{key!r}] is {uk[key]!r}, the contract froze {expected!r}"
        )


def test_single_series_and_assumption_free_spec_render_every_section(tmp_path: Path) -> None:
    """The N=1 and empty-`assumptions` edges the two-series fixture cannot reach.

    Two separate holes, both silent. At N=1 a report that dropped the overlay
    would still list images and still look complete, while carrying a hole in its
    own §8.5 inventory — and `charts.v1` §7.2.0 publishes the overlay
    unconditionally, so three is the correct count, not two. With
    `assumptions: []` a builder that treated the list as a reason to skip
    «Обмеження та припущення» would drop a whole section the contract froze,
    and a section that is present-but-empty reads as an oversight rather than as
    the honest answer.
    """
    before = _fixture_bytes()
    language = "uk"

    # --- behaviour 7: N=1 renders every section and exactly three images ---
    spec_path, metrics_path = _one_series_spec(tmp_path, assumptions=None)
    out_dir = tmp_path / "out.one"
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_one_series_csv(out_dir, "pl-post-przerywany")
    out_dir.joinpath("metrics.json").write_bytes(metrics_path.read_bytes())

    assert make_charts.main(["--spec", str(spec_path), "--out", str(out_dir)]) == 0
    charts = json.loads(out_dir.joinpath("charts.json").read_text(encoding="utf-8"))
    assert len(charts["charts"]) == 3, "N=1 publishes 2N+1 = 3 charts"

    assert _run_report(out_dir, spec_path) == 0
    text = out_dir.joinpath(build_report.REPORT_FILENAME).read_text(encoding="utf-8")

    # The SAME assertion the two-series tracer makes, so the single-series and
    # empty-assumption cases cannot quietly diverge from the frozen order.
    order = _section_order(text, language)
    assert order == sorted(order), f"the six sections are out of order at N=1: {order}"
    assert len(set(order)) == len(order)

    images = IMAGE_REF.findall(text)
    published = [entry["filename"] for entry in charts["charts"]]
    assert len(images) == 3, f"N=1 must render three images, rendered {len(images)}"
    assert images == published, "the N=1 image order drifted from charts.json"
    assert images[-1] == make_charts.OVERLAY_FILENAME, "the overlay is published at N=1 too"
    for name in images:
        assert (out_dir / name).is_file(), f"report.md references a missing PNG: {name}"

    # --- behaviour 8: an empty assumptions list removes the line, not the section ---
    empty_spec, empty_metrics = _one_series_spec(tmp_path, assumptions=[])
    out_empty = tmp_path / "out.no-assumptions"
    out_empty.mkdir(parents=True, exist_ok=True)
    _write_one_series_csv(out_empty, "pl-post-przerywany")
    out_empty.joinpath("metrics.json").write_bytes(empty_metrics.read_bytes())

    assert make_charts.main(["--spec", str(empty_spec), "--out", str(out_empty)]) == 0
    assert _run_report(out_empty, empty_spec) == 0
    text_empty = out_empty.joinpath(build_report.REPORT_FILENAME).read_text(encoding="utf-8")

    empty_order = _section_order(text_empty, language)
    assert empty_order == sorted(empty_order), "the empty-assumptions case broke the order"
    assert len(set(empty_order)) == len(empty_order), (
        "a builder that skipped the limitations section wholesale fails here"
    )
    # The only assertion a reworded assumption line cannot satisfy: the report's
    # own assumption token appears nowhere in the document at all.
    assumption_token = build_report.report_tokens(language)["assumption"]
    assert assumption_token not in text_empty, (
        "an empty assumptions list must render no assumption line"
    )

    assert _fixture_bytes() == before, "the run mutated a committed fixture"
