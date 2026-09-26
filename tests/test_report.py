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
import math
import re
from collections.abc import Mapping, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

import analyze_trends
import build_report
import common
import make_charts
from test_contracts import _iter_values


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# The committed document that reaches `confidence: "low"`, added by plan 06-02
# Task 1. Both committed metrics fixtures score `high` on every series, so
# without this one RPT-02's hypothesis framing is unreachable from committed
# data and would ship tested only by an in-test edit. It pins a DOCUMENT STATE,
# not a regeneration from any CSV: no `series.csv` reaches `low`, and this repo
# treats an unreproducible committed fixture as a defect only when it is claimed
# to be derived - which this one, by design, is not.
LOW_CONFIDENCE_EXAMPLE = FIXTURES_DIR / "metrics.low-confidence.example.json"

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


def _write_metrics(out_dir: Path, document: Mapping[str, Any] | str | bytes) -> Path:
    """Write `document` as `metrics.json` inside `out_dir`, and return the path.

    Bytes (or an already-serialized string) are written VERBATIM, which is what
    lets a test hand the report stage a committed fixture's own bytes rather
    than a re-serialization of the parsed document - a round-trip through
    `json.dumps` would silently normalize nothing today but is one more
    transformation between a committed fixture and the bytes a stage reads. A
    mapping is serialized for the derived documents Tasks 2 and 3 build.

    Always a file under `tmp_path`: no committed fixture is ever a write target.
    """
    path = out_dir / "metrics.json"
    if isinstance(document, bytes):
        path.write_bytes(document)
    elif isinstance(document, str):
        path.write_text(document, encoding="utf-8")
    else:
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


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


def _section_body(text: str, language: str, key: str) -> str:
    """The rendered body of one frozen section, from its heading to the next `## `.

    Scoped rather than whole-document on purpose: «Обмеження та припущення»
    renders a `- {label}: {seasonality note}` bullet in the same shape as a
    «Наскільки можна довіряти» bullet, so a whole-document search for
    `- {label}:` finds two lines and cannot say which section either is in.
    """
    tokens = build_report.report_tokens(language)
    start = text.index(f"## {tokens[key]}")
    remainder = text[start + 1 :]
    end = remainder.find("\n## ")
    return remainder if end == -1 else remainder[:end]


def _trust_line(text: str, label: str, language: str) -> str | None:
    """The one rendered «Наскільки можна довіряти» bullet for `label`, or None.

    Keyed on the label followed by a colon, which is the renderer's own shape
    for a trust bullet (`- {label}: {level} — {reasons}`), and searched ONLY
    inside the trust section - see `_section_body`. The label is passed in its
    rendered form; a label the renderer escaped is a separate edge (the
    GFM-escaping test), not this one.
    """
    prefix = f"- {label}:"
    matches = [
        line
        for line in _section_body(text, language, "trust").splitlines()
        if line.startswith(prefix)
    ]
    assert len(matches) <= 1, f"{len(matches)} trust lines for {label!r}: {matches!r}"
    return matches[0] if matches else None


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


def _low_confidence_series(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Every series in `document` whose confidence is exactly `low`."""
    return [node for node in document["series"] if node["confidence"] == "low"]


def test_low_confidence_metrics_fixture_matches_the_frozen_key_set() -> None:
    """The low-confidence document is the golden document, structurally.

    Every key set is compared KEY-FOR-KEY against `metrics.example.json` rather
    than against a count literal, so this tracks the frozen `metrics.v1` shape
    instead of a number this plan would have to keep re-deriving - the same
    comparison `test_contracts.py` makes for the spike-injected pair, and for
    the same reason: a fixture that quietly grew or lost a field would leave
    every other fixture test in the repo agreeing with a document no production
    code writes.

    The stronger claim is also pinned here, and it is the one that matters: the
    fixture's `confidence_reasons` is re-derived through
    `analyze_trends.score_confidence` from the fixture's OWN period, volume,
    anomaly share and clean-y1 availability. A hand-written reason list is a
    list no production code could have produced - the very fiction this test
    exists to refuse.
    """
    before = _fixture_bytes()
    assert LOW_CONFIDENCE_EXAMPLE.is_file(), (
        f"the low-confidence metrics fixture must be committed at "
        f"{LOW_CONFIDENCE_EXAMPLE.name} so RPT-02's hypothesis framing is "
        f"reachable from committed data rather than only from an in-test edit"
    )
    snapshot = {**before, LOW_CONFIDENCE_EXAMPLE.name: LOW_CONFIDENCE_EXAMPLE.read_bytes()}

    golden = json.loads((FIXTURES_DIR / "metrics.example.json").read_text(encoding="utf-8"))
    low = json.loads(LOW_CONFIDENCE_EXAMPLE.read_text(encoding="utf-8"))

    # Case 1 - the top-level key set, as a SET. No count literal anywhere here.
    assert set(low) == set(golden), (
        f"the low-confidence fixture's top-level keys must equal the golden's, "
        f"differing on {sorted(set(low) ^ set(golden))}"
    )

    # Case 2 - the same series ids in the same order. CONTRACTS.md §3: ids mirror
    # spec.series[] and a consumer must never see them reordered.
    assert [node["series_id"] for node in low["series"]] == [
        node["series_id"] for node in golden["series"]
    ], "the low-confidence fixture must keep the golden's series ids in the golden's order"

    for candidate, source in zip(low["series"], golden["series"]):
        assert set(candidate) == set(source), (
            f"{source['series_id']}: the low-confidence fixture's per-series key "
            f"set must equal the golden's, differing on "
            f"{sorted(set(candidate) ^ set(source))}"
        )

    # Case 3 - at least one series reaches `low`, and the reason list is the one
    # score_confidence emits for that state, with the hypothesis reason LAST.
    low_nodes = _low_confidence_series(low)
    assert low_nodes, (
        "the fixture exists to reach confidence: 'low'; no series in it does, so "
        "the hypothesis framing is still untestable from committed data"
    )
    for node in low_nodes:
        assert node["trend_direction"] in {"noise", "inconclusive"}, (
            f"{node['series_id']}: ANAL-04 forbids an up/down direction on a "
            f"low-confidence series, got {node['trend_direction']!r}"
        )
        clean_y1 = node["growth"]["y1"]["clean"]
        # `monthly_30d` is re-derived exactly the way `analyze_trends.build_metrics`
        # derives it - Fraction(total_views, period_days) * 30 - rather than from
        # the rounded `avg_daily_views`, because the rubric is compared against the
        # exact rational and a rounded proxy would put the fixture on the wrong side
        # of the 1000-view boundary it is built to sit under.
        level, reasons = analyze_trends.score_confidence(
            node["period"]["days"],
            Fraction(node["total_views"], node["period"]["days"]) * 30,
            node["anomaly_share"],
            clean_y1.get("pct") is not None,
            None,
        )
        assert node["confidence"] == level, (
            f"{node['series_id']}: the fixture says {node['confidence']!r} but "
            f"score_confidence says {level!r} for its own period/volume/anomalies"
        )
        assert node["confidence_reasons"] == reasons, (
            f"{node['series_id']}: the fixture's confidence_reasons is not the list "
            f"score_confidence emits for this state, so no production code could "
            f"have written it: {node['confidence_reasons']!r} != {reasons!r}"
        )
        assert node["confidence_reasons"][-1] == analyze_trends.LOW_CONFIDENCE_HYPOTHESIS_REASON, (
            "a 'low' document without the hypothesis reason last is a state "
            "score_confidence never emits"
        )

    # The fixture is worth its cost only if it also exercises the MIXED case: one
    # series stated as a conclusion, the other as a hypothesis, in one document.
    assert len(low_nodes) < len(low["series"]), (
        "every series is 'low', so the contrast with a 'high'/'medium' conclusion "
        "framing is untestable from this fixture"
    )

    # Case 4 - ANAL-06 on the new document: every null pct carries a non-empty
    # reason at its own level AND inside `clean`, and no float is non-finite.
    for node in low["series"]:
        for window in analyze_trends.GROWTH_WINDOWS:
            entry = node["growth"][window]
            if entry.get("pct") is None:
                reason = entry.get("reason")
                assert isinstance(reason, str) and reason.strip(), (
                    f"{node['series_id']}.{window}: a null pct with no reason of its "
                    f"own is the ANAL-06 defect this repo exists to prevent"
                )
                clean_reason = entry["clean"].get("reason")
                assert isinstance(clean_reason, str) and clean_reason.strip(), (
                    f"{node['series_id']}.{window}.clean: a null clean.pct with no "
                    f"reason is a bare 'n/a' on the page with nothing explaining it"
                )
    for value in _iter_values(low):
        if isinstance(value, float):
            assert math.isfinite(value), f"non-finite float in the fixture: {value!r}"

    # Case 6 - reading a fixture must not change one.
    assert snapshot == {
        **{name: (FIXTURES_DIR / name).read_bytes() for name in COMMITTED_FIXTURES},
        LOW_CONFIDENCE_EXAMPLE.name: LOW_CONFIDENCE_EXAMPLE.read_bytes(),
    }, "reading the fixtures changed their bytes"


def test_low_confidence_series_is_framed_as_a_hypothesis(tmp_out: Path) -> None:
    """A `low` series is framed as a hypothesis; a `high` one is a conclusion.

    Asserted on the RENDERED TEXT, never on a rendering, and against the
    localized phrase bound to `analyze_trends.LOW_CONFIDENCE_HYPOTHESIS_REASON`
    rather than against the English constant: a test asserting the constant
    would pass while the report printed that constant verbatim inside a
    Ukrainian document, which is exactly the bilingual artefact D-16 was written
    to make impossible.
    """
    before = _fixture_bytes()
    assert LOW_CONFIDENCE_EXAMPLE.is_file(), (
        "the low-confidence metrics fixture must be committed before the "
        "hypothesis framing can be tested from committed data"
    )
    low = json.loads(LOW_CONFIDENCE_EXAMPLE.read_text(encoding="utf-8"))
    low_nodes = _low_confidence_series(low)
    assert low_nodes, "the fixture must contain a low-confidence series"
    concluded = [node for node in low["series"] if node["confidence"] != "low"]
    assert concluded, "the fixture must also contain a concluded series for contrast"

    # The real chart stage first, so `charts.json` is a conforming sibling
    # manifest produced by the emitter rather than a hand-written one.
    _copy_chart_inputs(tmp_out)
    _write_metrics(tmp_out, LOW_CONFIDENCE_EXAMPLE.read_bytes())
    spec_path = FIXTURES_DIR / "spec.example.json"
    assert make_charts.main(["--spec", str(spec_path), "--out", str(tmp_out)]) == 0

    assert _run_report(tmp_out) == 0
    text = tmp_out.joinpath(build_report.REPORT_FILENAME).read_text(encoding="utf-8")

    language = str(json.loads((FIXTURES_DIR / "spec.example.json").read_text(encoding="utf-8"))["language"])
    hypothesis_token = build_report.report_tokens(language)["hypothesis"]
    localized_hypothesis_reason = build_report.reason_token(
        language, analyze_trends.LOW_CONFIDENCE_HYPOTHESIS_REASON
    )

    for node in low_nodes:
        assert localized_hypothesis_reason in text, (
            f"{node['series_id']}: the report must localize the hypothesis reason"
        )
        line = _trust_line(text, str(node["label"]), language)
        assert line is not None, f"no «{node['series_id']}» trust line was rendered"
        assert hypothesis_token in line, (
            f"a 'low' series is stated as a conclusion: {line!r} carries no "
            f"hypothesis token {hypothesis_token!r}"
        )
        assert localized_hypothesis_reason in line, (
            f"the hypothesis framing does not name its reason on the same line: {line!r}"
        )

    for node in concluded:
        line = _trust_line(text, str(node["label"]), language)
        assert line is not None, f"no «{node['series_id']}» trust line was rendered"
        assert hypothesis_token not in line, (
            f"{node['series_id']} is {node['confidence']!r} and must be stated as a "
            f"conclusion, yet its line carries the hypothesis framing: {line!r}"
        )

    assert _fixture_bytes() == before, "the run mutated a committed fixture"
