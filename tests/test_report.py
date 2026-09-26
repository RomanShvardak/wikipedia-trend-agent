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

# --- 06-02 Task 2: the QA-02 fidelity battery --------------------------------
#
# The seam below is the reverse-fidelity direction CONTRACTS.md §8.6 names and no
# test owned before this plan: that no number in the MARKDOWN lacks a source in
# the documents. `metrics_shown` proves the forward direction (everything
# required was displayed); nothing proved the reverse, so a number invented by a
# renderer would have shipped with a manifest that never mentioned it.
#
# One function, importable and callable from OUTSIDE pytest with a plain
# markdown string and a plain source mapping, because 06-03's non-vacuity probe
# drives it directly. It is not buried inside a test.

# A maximal run of digits, commas and dots. Commas are captured rather than
# excluded so a grouping separator is part of the token and can be normalized
# OFF the Markdown side - CONTRACTS.md §8.4's non-locale-aware digits reach the
# page as `1,720,628`, and a pattern that stopped at the comma would read
# `1` and `720` and `628` as three separate numbers.
DIGIT_RUN = re.compile(r"\d[\d,.]*")
# A numeric literal embedded in a STRING value of a source document.
STRING_NUMBER = re.compile(r"\d[\d,.]*")
# The two date shapes the documents carry: `YYYY-MM-DD` and the compact
# `YYYYMMDD` the analyzer's own anomaly shape uses.
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
COMPACT_DATE = re.compile(r"^\d{8}$")

# The per-series fields the report is required to display, in the order the
# metrics table renders them. The manifest assertion builds its expected set from
# this tuple AND the fixture's own series list, so neither a count literal nor a
# hard-coded index can rot: a new display field is a one-line edit here, and a
# field the report silently stopped showing fails the comparison.
REQUIRED_SHOWN_FIELDS = (
    "label",
    "project",
    "article",
    "language",
    "period.start",
    "period.end",
    "period.days",
    "total_views",
    "avg_daily_views",
    "growth.m3.clean.pct",
    "growth.y1.clean.pct",
    "growth.y2.clean.pct",
    "trend_direction",
    "confidence",
    "confidence_reasons",
    "anomaly_share",
)

# Words that would turn a measurement into a projection. Matched against the
# document with the `not_a_forecast` phrase REMOVED first, because that phrase is
# the report's own disclaimer and contains the very word ("прогноз" / "forecast")
# the prohibition is about: a naive substring search for it would fail on a
# correct report and pass on one that dropped the disclaimer entirely - the
# exact inversion the disclaimer exists to prevent.
FORECAST_VOCABULARY = (
    # the report language's own words
    "попит",
    "виручка",
    "дохід",
    "прибуток",
    "конверс",
    "прогноз",
    "платити",
    "оплатити",
    # the verbatim English constants, which are NOT redundant: an English
    # projection sentence leaking into a localized report is precisely the
    # failure this assertion exists to catch
    "demand",
    "revenue",
    "willingness to pay",
    "ability to pay",
    "forecast",
    "conversion",
    "monetiz",
)

# A CLOSED list, and the closure is the point. The CR-01 defect was that a
# rendered Ukrainian document printed `low`, `up`, `inconclusive` and the three
# ASCII chart-disclosure keys verbatim, and the goal is that THESE EXACT ELEVEN
# WORDS do not appear - not that no Latin text appears at all. Two legitimate
# ASCII things do appear in a `uk` report and are deliberately not scanned: the
# chart `filename`s, and the overlay's frozen `charts.v1 §7.2.4` ASCII `note`,
# which is a stable interface string quoted verbatim. A broad "no English" gate
# would have to be weakened to pass, and a gate that must be weakened to pass is
# a gate nobody trusts - so the closed list is narrower, exact, and honest about
# what it excludes.
ENGLISH_ENUM_AND_DISCLOSURE_VOCABULARY: tuple[str, ...] = (
    "low",
    "medium",
    "high",
    "up",
    "down",
    "flat",
    "noise",
    "inconclusive",
    "gaps",
    "anomalies_drawn",
    "log_masked_points",
)

# A standalone `inf` / `nan` as a word. Substring matching would be wrong here
# (`inf` is inside `information`), which is why these two are word-bounded while
# `None` / `null` / `NaN` / `Infinity` are matched as literals - no English word
# in either table contains them.
LEAK_LITERALS = ("None", "null", "nan", "NaN", "Infinity", "-Infinity")
LEAK_WORDS = ("inf", "nan")


def _iter_leaf_values(node: object):
    """Yield every non-container leaf reachable in a JSON document."""
    if isinstance(node, Mapping):
        for value in node.values():
            yield from _iter_leaf_values(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _iter_leaf_values(value)
    else:
        yield node


def _add_date_components(text: str, into: set[float]) -> None:
    """Add the year / month / day of a date string, so a date is matchable.

    A rendered `2026-09-20` reaches the reader as three numeral runs, and each
    must trace to the document that published the date. Adding the components
    rather than the whole date is deliberate: a comparison that accepted only
    the assembled date could not tell `2026-09-20` from `2020-09-26`.
    """
    if ISO_DATE.match(text):
        year, month, day = text.split("-")
    elif COMPACT_DATE.match(text):
        year, month, day = text[0:4], text[4:6], text[6:8]
    else:
        return
    for part in (year, month, day):
        into.add(float(int(part)))


def _source_numbers(documents: Sequence[Mapping[str, Any]]) -> set[float]:
    """Every finite value a numeral in the Markdown could legitimately be.

    Three sources, and the reasoning for each matters because the whole check is
    only as strong as the looseness of this function:

    1. **Every finite number reachable in the documents.** Taken verbatim - no
       rounding, no tolerance, no stripping. This is the direction the plan
       insists is never loosened.
    2. **Every numeric literal inside every string value of the documents.** The
       documents carry English reason constants (`"monthly 30-day views at least
       10000"`) that the report LOCALIZES, and a localized phrase can legitimately
       contain a numeral the metrics document never held as a number. Excluding
       them would make the check fail on a correct report, and a gate that must
       be weakened to pass is a gate nobody trusts. This widens the set only by
       numerals the documents themselves contain, so an invented number still
       has no source.
    3. **The year / month / day of every date string**, per `_add_date_components`.
    """
    values: set[float] = set()
    for document in documents:
        for leaf in _iter_leaf_values(document):
            if isinstance(leaf, bool):
                continue
            if isinstance(leaf, (int, float)):
                if math.isfinite(float(leaf)):
                    values.add(float(leaf))
            elif isinstance(leaf, str):
                _add_date_components(leaf, values)
                for run in STRING_NUMBER.findall(leaf):
                    cleaned = run.rstrip(",.").replace(",", "")
                    if cleaned:
                        values.add(float(cleaned))
    return values


def _markdown_numeric_tokens(markdown: str) -> list[str]:
    """Every numeral in `markdown` that is a MEASUREMENT rather than a name.

    Two constructs in the rendered document look numeric and are not:

    - a **percent-escape** inside a percent-encoded article slug
      (`P%C5%AFst_p%C5%99eru%C5%A1ovan%C3%BD`). `5`, `99`, `1` and `3` are character
      codes, not measurements, and they are not in any source document as
      numbers. A run immediately preceded by `%` is skipped.
    - a **window label** (`3M`, `1Y`, `2Y`). `1`, `2` and `3` name a window, and
      the report deliberately never states them as `metrics.v1` window lengths.
      A run immediately followed by a letter or underscore is skipped.

    A trailing `%` is a percentage and is KEPT - the value is the measurement.
    """
    tokens: list[str] = []
    for match in DIGIT_RUN.finditer(markdown):
        start, end = match.span()
        before = markdown[start - 1] if start > 0 else ""
        after = markdown[end] if end < len(markdown) else ""
        if before == "%" or (before.isalnum() and before.isascii() and not before.isdigit()):
            continue
        if after == "%":
            after = ""
        if after.isascii() and (after.isalpha() or after == "_"):
            continue
        tokens.append(match.group())
    return tokens


def assert_no_invented_numbers(markdown: str, sources: Mapping[str, Any]) -> None:
    """No numeral in `markdown` may lack a source in `sources`.

    Callable from outside pytest with plain arguments - that is the point, and
    06-03's non-vacuity probe drives it exactly this way to show the check goes
    red on an injected wrong number rather than passing on everything.

    Normalization happens on the MARKDOWN side only: grouping separators are
    removed, a trailing `%` and a leading sign are dropped, and the remainder is
    coerced to a float. Nothing is stripped, rounded or tolerated on the source
    side, so the check cannot be made to pass by loosening the documents. A token
    that cannot be coerced is itself a FAILURE and is named in the message: a
    number the report cannot even parse is a number nothing vouches for.
    """
    available = _source_numbers(list(sources.values()))
    for raw in _markdown_numeric_tokens(markdown):
        # `..` is the range separator the period cell renders between two dates
        # (`2024-09-23..2026-09-20`), so a maximal run can straddle it. Split on
        # it rather than letting one range swallow both of its endpoints.
        for piece in raw.split(".."):
            cleaned = piece.replace(",", "").rstrip(".")
            if not cleaned:
                continue
            try:
                value = float(cleaned)
            except ValueError:
                raise AssertionError(
                    f"the rendered report carries the numeric token {piece!r}, "
                    f"which is not a number at all - nothing vouches for it"
                ) from None
            if value not in available:
                raise AssertionError(
                    f"the rendered report carries {piece!r} ({value!r}), which appears "
                    f"in no value of any source document: an invented number"
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
    #
    # The filter is built from the TOKEN, not from the ASCII literal
    # `anomalies_drawn=`: 06-04 made the disclosure key a `REPORT_TOKENS` entry,
    # so a filter hard-coded on the English key would match zero lines and the
    # distinctness property - the one that caught the original duplicate-line
    # defect - would be asserted over an empty list. Re-keying is not a licence
    # to weaken any of the four properties below; all four are kept verbatim.
    disclosure_key = build_report.report_tokens("uk")["disclosure_anomalies_drawn"]
    disclosures = [line for line in text.splitlines() if disclosure_key in line]
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


# --- 06-04 Task 1: the CR-01 tracer ------------------------------------------
#
# 06-VERIFICATION.md recorded B-1 / CR-01: a rendered Ukrainian report printed
# `up`, `high` and `anconclusible`-class enum values plus the three ASCII
# chart-disclosure keys, because `REPORT_TOKENS` had no key for an enum value and
# a `low` word is not a number, so §8.4's sweep and every arithmetic test were
# blind to it. `test_uk_report_carries_no_english_enum_or_disclosure_value` is
# the witness: it renders the committed fixture through the REAL chart stage,
# reads `report.md` back OFF DISK, and asserts the closed eleven-word list is
# absent from the three regions the defect reached. Its own name carries
# `english_enum` because Task 4's non-vacuity probe 1 selects it with
# `-k english_enum` - measured on this project's pytest, a selector matching no
# node name prints `50 deselected in 0.08s` and exits 5, which is non-zero, so
# the name is a pinned selector contract and not a description.
def test_uk_report_carries_no_english_enum_or_disclosure_value(tmp_out: Path) -> None:
    """A `uk` report carries no English enum value, disclosure key or trust level.

    Three regions and three, because a fourth would be dishonest rather than
    stricter: the CHART section is deliberately NOT scanned, since it carries the
    chart `filename`s and the overlay's frozen `charts.v1 §7.2.4` ASCII `note`,
    which is a stable interface string quoted verbatim from the manifest and which
    `06-VERIFICATION.md` lists as a named human call rather than as a gap. Those
    are the scope exclusions, stated here rather than left implicit.

    POSITIVES FIRST, absences second. Every absence assertion below is trivially
    satisfiable by an empty or broken document, so the six headings, a non-empty
    `metrics_shown` and at least one rendered `format_number(..., percent=True)`
    are all asserted BEFORE the first `not re.search(...)`, and the count of rows
    and regions actually inspected is asserted non-zero so a rename that emptied
    the metrics table cannot make the loop vacuous.
    """
    before = _fixture_bytes()
    assert LOW_CONFIDENCE_EXAMPLE.is_file(), (
        "the low-confidence metrics fixture must be committed before the "
        "localization can be tested from committed data"
    )
    _copy_chart_inputs(tmp_out)
    _write_metrics(tmp_out, LOW_CONFIDENCE_EXAMPLE.read_bytes())
    spec_path = FIXTURES_DIR / "spec.example.json"
    assert make_charts.main(["--spec", str(spec_path), "--out", str(tmp_out)]) == 0
    assert _run_report(tmp_out) == 0

    text = tmp_out.joinpath(build_report.REPORT_FILENAME).read_text(encoding="utf-8")
    manifest = json.loads(
        tmp_out.joinpath(build_report.REPORT_MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    language = _spec_language()

    # --- positives ---
    order = _section_order(text, language)
    assert order == sorted(order), f"the six sections are out of order: {order}"
    assert len(set(order)) == len(order), "a section heading appears more than once"
    assert manifest["metrics_shown"], "metrics_shown is empty, so nothing was rendered"

    document = json.loads(LOW_CONFIDENCE_EXAMPLE.read_text(encoding="utf-8"))
    clean_phrases = [
        build_report.format_number(node["growth"][window]["clean"]["pct"], percent=True)
        for node in document["series"]
        for window in analyze_trends.GROWTH_WINDOWS
        if node["growth"][window]["clean"].get("pct") is not None
    ]
    assert clean_phrases, "the fixture has no non-null clean percentage to render"
    assert any(f"{phrase}%" in text for phrase in clean_phrases), (
        f"no clean percentage was rendered at all, so the absence assertions below "
        f"would be satisfiable by an empty document: {clean_phrases!r}"
    )

    # --- absences ---
    rows = _metrics_table_rows(text, language)
    regions = [
        *rows,
        _section_body(text, language, "trust"),
        _section_body(text, language, "limitations"),
    ]
    assert rows, "the metrics table has no data rows, so nothing was inspected"
    assert len(regions) == len(rows) + 2 and all(region for region in regions), (
        f"expected one region per metrics row plus trust and limitations, got {regions!r}"
    )
    for index, region in enumerate(regions):
        for word in ENGLISH_ENUM_AND_DISCLOSURE_VOCABULARY:
            found = re.search(rf"\b{re.escape(word)}\b", region, re.IGNORECASE)
            assert found is None, (
                f"region {index} carries the English word {word!r} at offset "
                f"{found.start() if found else -1}: {region!r}"
            )

    assert _fixture_bytes() == before, "the run mutated a committed fixture"


def test_confidence_and_trend_tokens_refuse_an_unmapped_enum_value() -> None:
    """No enum value can fall back to the English word, and the refusal names it.

    Modelled on `test_reason_token_refuses_an_unmapped_reason`, and the same two
    halves. First, an out-of-enum value raises and NAMES the value verbatim, so a
    caller knows which upstream value to add - which is also what makes an added
    value loud instead of silent. Second, every mapped value resolves to a
    non-blank phrase in EVERY declared language: a blank phrase would render an
    empty cell and still satisfy a membership check.
    """
    for lookup, value in (
        (build_report.confidence_token, "very high"),
        (build_report.trend_token, "sideways"),
    ):
        with pytest.raises(build_report.ReportError) as refusal:
            lookup("uk", value)
        assert value in str(refusal.value), (
            f"the refusal must name the offending value verbatim; got {refusal.value!r}"
        )

    for language in build_report.REPORT_TOKENS:
        for mapping in (build_report.CONFIDENCE_TOKEN_KEYS, build_report.TREND_TOKEN_KEYS):
            for value, key in mapping.items():
                phrase = build_report._enum_token(language, mapping, value, "enum")
                assert phrase.strip(), (
                    f"REPORT_TOKENS[{language!r}][{key!r}] is blank, so the value "
                    f"{value!r} would render as an empty cell"
                )
                assert phrase == build_report.report_tokens(language)[key]


def test_enum_token_maps_cover_every_value_analyze_trends_can_emit() -> None:
    """Both maps are complete against the code that PRODUCES the values, measured.

    `REASON_TOKENS` keys its reasons by importing the upstream constant BY
    REFERENCE, so an upstream rename fails at collection time. The two enum
    families cannot: `analyze_trends.py:729/737/739/740/752/754/756/759` hold
    INLINE string literals, so there is nothing to import - which is exactly why
    the CR-01 leak survived, since no upstream rename could ever break a key set.

    This test is the mechanical substitute. It CALLS `score_confidence` and
    `safe_direction` over their documented input grids, collects what they
    actually return, and requires the measured set to equal the literal enum set
    AND the map's key set. A new level or direction added upstream without a
    token fails here instead of leaking English onto the page. The non-empty
    result count is asserted per grid so an argument-order mistake cannot empty
    either set and make the equality vacuously true.
    """
    from datetime import date

    levels: set[str] = set()
    level_calls = 0
    for period_days in (30, 91, 730):
        for monthly_30d in (0, 500, 1000, 10000):
            for anomaly_share in (0.0, 0.05, 0.5):
                for clean_y1_available in (True, False):
                    for comparison_span in (None, (date(2014, 1, 1), date(2016, 1, 1))):
                        level, _reasons = analyze_trends.score_confidence(
                            period_days,
                            monthly_30d,
                            anomaly_share,
                            clean_y1_available,
                            comparison_span,
                        )
                        levels.add(level)
                        level_calls += 1
    assert level_calls == 3 * 4 * 3 * 2 * 2, "the confidence grid was not fully exercised"
    assert levels == {"low", "medium", "high"}, (
        f"score_confidence's reachable levels drifted: {sorted(levels)}"
    )
    assert levels == set(build_report.CONFIDENCE_TOKEN_KEYS), (
        f"CONFIDENCE_TOKEN_KEYS must cover every reachable level, differing on "
        f"{sorted(levels ^ set(build_report.CONFIDENCE_TOKEN_KEYS))}"
    )

    directions: set[str] = set()
    direction_calls = 0
    for period_days in (30, 730):
        for raw_y1_pct in (None, -20.0, 0.0, 20.0):
            for clean_y1_pct in (None, -20.0, 0.0, 20.0):
                for confidence in ("low", "high"):
                    for monthly_30d in (500, 5000):
                        directions.add(
                            analyze_trends.safe_direction(
                                period_days,
                                raw_y1_pct,
                                clean_y1_pct,
                                confidence,
                                monthly_30d,
                            )
                        )
                        direction_calls += 1
    assert direction_calls == 2 * 4 * 4 * 2 * 2, "the direction grid was not fully exercised"
    assert directions == {"up", "down", "flat", "noise", "inconclusive"}, (
        f"safe_direction's reachable directions drifted: {sorted(directions)}"
    )
    assert directions == set(build_report.TREND_TOKEN_KEYS), (
        f"TREND_TOKEN_KEYS must cover every reachable direction, differing on "
        f"{sorted(directions ^ set(build_report.TREND_TOKEN_KEYS))}"
    )


def test_report_token_tables_disagree_for_the_eleven_new_keys() -> None:
    """The `uk` phrases are real translations, not copies of the `en` ones.

    Without this, `test_uk_report_carries_no_english_enum_or_disclosure_value`
    would be satisfiable by a `uk` table silently filled from the `en` table -
    the absence assertions would then be unearnable rather than earned, and the
    CR-01 test would pass on a bilingual document. The second half (no ASCII
    letter in the `uk` value) is what makes "translated" mechanical rather than
    a matter of taste.
    """
    keys = (
        "confidence_low",
        "confidence_medium",
        "confidence_high",
        "trend_up",
        "trend_down",
        "trend_flat",
        "trend_noise",
        "trend_inconclusive",
        "disclosure_gaps",
        "disclosure_anomalies_drawn",
        "disclosure_log_masked_points",
    )
    for key in keys:
        assert build_report.REPORT_TOKENS["uk"][key] != build_report.REPORT_TOKENS["en"][key], (
            f"REPORT_TOKENS['uk'][{key!r}] is a copy of the English phrase, so the "
            f"closed-list absence assertions are unearned"
        )
        assert not re.search(r"[A-Za-z]", build_report.REPORT_TOKENS["uk"][key]), (
            f"REPORT_TOKENS['uk'][{key!r}] contains an ASCII letter: "
            f"{build_report.REPORT_TOKENS['uk'][key]!r}"
        )


# --- 06-02 Task 2: the fidelity battery --------------------------------------


def _spec_language() -> str:
    """The DOCUMENT language, read from the committed spec rather than hardcoded."""
    return str(json.loads((FIXTURES_DIR / "spec.example.json").read_text(encoding="utf-8"))["language"])


def _render_two_series(tmp_out: Path) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Render the committed two-series document and read all three artifacts back.

    `charts.json` comes from the REAL chart stage, and the returned report text is
    read from DISK rather than taken from `render_report`'s return value: a
    fidelity check that reads the planner's output would prove the planner
    formats what it intends, while the published file is what a reader opens.
    """
    before = _fixture_bytes()
    _render_charts(tmp_out)
    assert _run_report(tmp_out) == 0
    text = tmp_out.joinpath(build_report.REPORT_FILENAME).read_text(encoding="utf-8")
    metrics = json.loads(tmp_out.joinpath("metrics.json").read_text(encoding="utf-8"))
    charts = json.loads(tmp_out.joinpath("charts.json").read_text(encoding="utf-8"))
    manifest = json.loads(
        tmp_out.joinpath(build_report.REPORT_MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    assert _fixture_bytes() == before, "the run mutated a committed fixture"
    return text, metrics, charts, manifest


def _footer(text: str) -> str:
    """Everything after the document's last horizontal rule - the footer block.

    Scoped rather than whole-document because «Наступний крок» renders the SAME
    `not_a_forecast` phrase the footer does. An injected-defect probe made that
    concrete: deleting the footer's clause entirely left the whole-document
    assertion green (observed: `1 passed, 16 deselected`), because the next-step
    line still carried the phrase. The prohibition is specifically that the
    FOOTER states what the report is not, so the assertion has to read the
    footer.
    """
    marker = "\n---\n"
    index = text.rfind(marker)
    assert index != -1, "the rendered document has no closing horizontal rule"
    return text[index + len(marker) :]


def _metrics_table_header(text: str, language: str) -> str:
    """The metrics table's header row, located by its FIRST column header."""
    tokens = build_report.report_tokens(language)
    prefix = f"| {tokens['series']} |"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"no metrics table header starting {prefix!r} was rendered")


def _metrics_table_rows(text: str, language: str) -> list[str]:
    """The metrics table's DATA rows, in published order.

    Starts after the header AND its delimiter row, and stops at the first line
    that is not a row - so a label that broke out of the table would shorten this
    list rather than silently extend it, and the row-count assertion below is
    what notices.
    """
    header = _metrics_table_header(text, language)
    lines = text.splitlines()
    start = lines.index(header)
    rows: list[str] = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        rows.append(line)
    return rows


def _unseparated_pipes(line: str) -> int:
    """The count of REAL cell separators in a table line.

    A `\\|` is an escaped pipe INSIDE a cell, so counting raw `|` characters would
    report a correctly escaped label as a broken table. The lookbehind is the
    whole measurement: a cell that split shifts this count, and a cell that did
    not keeps it equal to the header's.
    """
    return len(re.findall(r"(?<!\\)\|", line))


def test_no_invented_numbers_in_the_rendered_report(tmp_out: Path) -> None:
    """Every numeral in the rendered Markdown is a value from a source document.

    The reverse of `metrics_shown`. That list proves everything required was
    displayed; nothing proved the opposite, so a renderer that computed a
    percentage of its own would have produced a perfectly well-formed document
    whose manifest never mentioned the number. No tolerance, no rounding slack
    and no assertion on prose: one numeral with no source fails here.
    """
    text, metrics, charts, _manifest = _render_two_series(tmp_out)

    assert_no_invented_numbers(
        text,
        {
            "metrics.json": metrics,
            "charts.json": charts,
        },
    )


def test_metrics_shown_equals_the_frozen_required_set(tmp_out: Path) -> None:
    """The published `metrics_shown` is the required set, built from the fixture.

    The expected set is derived from the golden fixture's OWN series list crossed
    with `REQUIRED_SHOWN_FIELDS`, so there is no count literal anywhere: a
    `metrics.v1` growth does not break this test, and a report that quietly
    stopped displaying a required field does.
    """
    _text, metrics, _charts, manifest = _render_two_series(tmp_out)
    required = [
        f"series[{index}].{field}"
        for index, node in enumerate(metrics["series"])
        for field in REQUIRED_SHOWN_FIELDS
    ]
    shown = manifest["metrics_shown"]

    assert shown, "metrics_shown is empty: the report published no metric pointer at all"
    assert len(shown) == len(set(shown)), (
        f"metrics_shown carries duplicates, so a consumer's set comparison would "
        f"depend on how many sections quoted the same value: {shown}"
    )
    assert set(shown) == set(required), (
        f"metrics_shown does not equal the required set; "
        f"missing={sorted(set(required) - set(shown))} extra={sorted(set(shown) - set(required))}"
    )

    # ORDER, as two separate properties, because §8.6's list is ordered by the
    # order the DOCUMENT rendered (section by section), not grouped by series -
    # asserting contiguity per series would be asserting a design the contract
    # does not have.
    #
    # (a) the table rendered in `metrics.series[]` order, read off the label
    #     pointers, which the table emits one per row in that order;
    # (b) the list is a deterministic function of its inputs, i.e. two runs over
    #     the same documents publish the same list. This is RPT-02's ordering
    #     truth stated as a check: a renderer that sorted by `pct` or by
    #     `confidence` would keep every other assertion in this module green
    #     while making the document unstable between runs.
    label_pointers = [pointer for pointer in shown if pointer.endswith(".label")]
    assert [int(pointer.split("[")[1].split("]")[0]) for pointer in label_pointers] == sorted(
        int(pointer.split("[")[1].split("]")[0]) for pointer in label_pointers
    ), f"the table rows do not follow metrics.series[] order: {label_pointers}"

    repeat_dir = tmp_out.parent / "repeat"
    repeat_dir.mkdir(parents=True, exist_ok=True)
    _text2, _m2, _c2, manifest2 = _render_two_series(repeat_dir)
    assert manifest2["metrics_shown"] == shown, (
        "two runs over the same documents published different metrics_shown lists; "
        "the published order is not a function of the inputs"
    )


def test_null_growth_renders_not_computable_with_its_reason(tmp_out: Path) -> None:
    """A null `clean.pct` is the not-computable token plus ITS OWN clean reason.

    ANAL-06 is the rule this whole repository exists to keep, and the reading it
    forbids is specific: a reader who sees `0` concludes "no growth", where the
    truth is "not measured". The cell is therefore checked three ways at once -
    it carries the localized not-computable token, it carries the reason that
    belongs to the CLEAN variant (the table displays the clean variant, so the
    window-level reason would be a different sentence's explanation), and it does
    not degenerate into a bare `0` / `0.0`.
    """
    text, metrics, _charts, _manifest = _render_two_series(tmp_out)
    language = _spec_language()
    tokens = build_report.report_tokens(language)
    rows = _metrics_table_rows(text, language)
    assert len(rows) == len(metrics["series"]), (
        f"one rendered row per series expected, got {len(rows)} for "
        f"{len(metrics['series'])}"
    )

    checked = 0
    for index, node in enumerate(metrics["series"]):
        row = rows[index]
        for window in analyze_trends.GROWTH_WINDOWS:
            entry = node["growth"][window]
            if entry["clean"].get("pct") is not None:
                continue
            checked += 1
            reason = build_report.reason_token(language, str(entry["clean"]["reason"]))
            phrase = (
                f"{make_charts.WINDOW_LABELS[window]} "
                f"{tokens['not_computable']} ({build_report.md_cell(reason)})"
            )
            assert phrase in row, (
                f"series[{index}].{window}: the null window's cell must carry the "
                f"not-computable token and its own clean.reason; expected {phrase!r}"
            )
            cell = row.split(f"{make_charts.WINDOW_LABELS[window]} ", 1)[1].split(" / ", 1)[0]
            assert not re.fullmatch(r"[-+]?0(\.0)?%?", cell.strip()), (
                f"series[{index}].{window}: a null window rendered as {cell!r} - a "
                f"reader would conclude 'no growth' where the truth is 'not measured'"
            )
    assert checked, (
        "no null growth window was reached, so this test proved nothing about the "
        "not-computable rendering"
    )
    assert "None" not in text, "the literal None leaked into the document"
    assert "null" not in text, "the literal null leaked into the document"

    # --- the CLEAN reason specifically, not merely "a reason" ---------------
    #
    # The golden fixture's window-level and clean-level reasons are the SAME
    # string, so the assertion above cannot tell them apart. An injected-defect
    # probe made that concrete: swapping `clean.reason` for the window-level
    # `reason` inside `_growth_phrase` left this test GREEN (observed:
    # `1 passed, 16 deselected`), and the rendered row under that defect really
    # did carry the wrong phrase. A gate never shown to fail has not been shown
    # to pass, and the distinction is the whole point: the table displays the
    # CLEAN variant, so the clean variant's explanation is the one that belongs
    # in its cell. This sub-case derives a document whose two reasons DIFFER, so
    # the assertion can tell them apart at all.
    split_dir = tmp_out.parent / "split-reasons"
    split_dir.mkdir(parents=True, exist_ok=True)
    split = json.loads((FIXTURES_DIR / "metrics.example.json").read_text(encoding="utf-8"))
    split["series"][0]["growth"]["y2"]["reason"] = analyze_trends.MISSING_CLEAN_Y1_REASON
    _copy_chart_inputs(split_dir)
    _write_metrics(split_dir, split)
    assert make_charts.main(
        ["--spec", str(FIXTURES_DIR / "spec.example.json"), "--out", str(split_dir)]
    ) == 0
    assert _run_report(split_dir) == 0
    split_text = split_dir.joinpath(build_report.REPORT_FILENAME).read_text(encoding="utf-8")
    split_row = _metrics_table_rows(split_text, language)[0]

    clean_phrase = build_report.reason_token(
        language, analyze_trends.INSUFFICIENT_OBSERVATIONS_REASON
    )
    window_phrase = build_report.reason_token(language, analyze_trends.MISSING_CLEAN_Y1_REASON)
    assert clean_phrase != window_phrase, (
        "the two reason constants this sub-case depends on must be distinguishable, "
        "or the assertion below cannot discriminate"
    )
    assert build_report.md_cell(clean_phrase) in split_row, (
        f"the null cell must carry THAT window's own clean.reason "
        f"({clean_phrase!r}); the row reads {split_row!r}"
    )
    assert build_report.md_cell(window_phrase) not in split_row, (
        f"the null cell carries the window-level reason instead of the clean one "
        f"({window_phrase!r}) - the table reports the clean variant, so the clean "
        f"variant's explanation is the one that belongs there"
    )


def test_zero_growth_is_distinct_from_not_computable(tmp_out: Path) -> None:
    """A `clean.pct` of exactly `0.0` is a measured flat window, not an absence.

    One step from the null case, and the assertion that keeps ANAL-06's "never
    zero" from being misread as "never a zero": a report that refused to print
    `+0.0%` because zero looks like the null sentinel would be replacing one
    misreading with another - a genuinely flat series reported as unmeasured.
    Both directions are checked on the SAME row, so the two renderings are shown
    to be structurally distinct rather than merely described as such.
    """
    window = "y1"
    document = json.loads((FIXTURES_DIR / "metrics.example.json").read_text(encoding="utf-8"))
    entry = document["series"][0]["growth"][window]
    entry["pct"] = 0.0
    entry["abs"] = 0
    entry["clean"]["pct"] = 0.0
    entry["clean"]["abs"] = 0

    _copy_chart_inputs(tmp_out)
    _write_metrics(tmp_out, document)
    assert make_charts.main(
        ["--spec", str(FIXTURES_DIR / "spec.example.json"), "--out", str(tmp_out)]
    ) == 0
    assert _run_report(tmp_out) == 0

    language = _spec_language()
    tokens = build_report.report_tokens(language)
    text = tmp_out.joinpath(build_report.REPORT_FILENAME).read_text(encoding="utf-8")
    row = _metrics_table_rows(text, language)[0]

    signed_zero = f"{make_charts.WINDOW_LABELS[window]} +0.0%"
    assert signed_zero in row, (
        f"a measured 0.0% must render with its sign and its percent mark, "
        f"expected {signed_zero!r} in the row"
    )
    zero_cell = row.split(f"{make_charts.WINDOW_LABELS[window]} ", 1)[1].split(" / ", 1)[0]
    assert tokens["not_computable"] not in zero_cell, (
        f"a measured 0.0% rendered as {zero_cell!r} - the two states must be "
        f"structurally distinct, not merely differently worded"
    )
    # And the same row's genuinely-null window still reads as not computable.
    assert f"{make_charts.WINDOW_LABELS['y2']} {tokens['not_computable']}" in row, (
        "the null window on the same row lost its not-computable rendering"
    )


def test_every_dynamic_conclusion_states_its_window_pct_and_volume_base(tmp_out: Path) -> None:
    """Every conclusion line carries its window, its percentage AND its base.

    A growth percentage with no stated volume base is unreadable: `+16.7%` is a
    very different finding at 100 views/day than at 2,363. The base is the
    series' OWN `avg_daily_views`, read from `metrics.json` and never recomputed
    (§7.3), and the plan's own correction for this task is that the base must sit
    on the SAME line as the percentage rather than in a separate table row where
    a reader could lose the pairing.
    """
    text, metrics, _charts, _manifest = _render_two_series(tmp_out)
    language = _spec_language()
    tokens = build_report.report_tokens(language)
    body = _section_body(text, language, "conclusion")

    checked = 0
    for node in metrics["series"]:
        label = build_report.md_cell(node["label"])
        base = build_report.format_number(node["avg_daily_views"])
        for window in analyze_trends.GROWTH_WINDOWS:
            entry = node["growth"][window]
            if entry["clean"].get("pct") is None:
                continue
            checked += 1
            pct = build_report.format_number(entry["clean"]["pct"], percent=True)
            line = next(
                (
                    candidate
                    for candidate in body.splitlines()
                    if candidate.startswith(f"- {label} · {make_charts.WINDOW_LABELS[window]} ")
                ),
                None,
            )
            assert line is not None, (
                f"no conclusion line for {node['series_id']} {window}"
            )
            assert make_charts.WINDOW_LABELS[window] in line
            assert f"{pct}%" in line, (
                f"the conclusion line states no percentage through the single "
                f"formatting seam: {line!r} lacks {pct!r}"
            )
            assert f"{base} {tokens['avg_daily_views']}" in line, (
                f"the conclusion line does not state the series' own "
                f"avg_daily_views {base!r} on the same line: {line!r}"
            )
    assert checked, "no computable growth window was reached, so this proved nothing"


def test_no_none_nan_or_null_leak_paired_with_a_positive_assertion(tmp_out: Path) -> None:
    """Substring absences, each paired with a positive assertion in THIS body.

    RESEARCH Pitfall 4's all-negative-test trap: a builder that crashed into an
    empty file satisfies every "X is absent" assertion at once, so the absences
    below are worthless alone. Each is therefore stated in the same test body as
    the six headings being present, the manifest being published and
    `metrics_shown` being non-empty - so a broken builder fails the positives and
    a working one has to earn the negatives.
    """
    text, _metrics, _charts, manifest = _render_two_series(tmp_out)
    language = _spec_language()

    # --- the positives, first: without these the absences prove nothing ---
    order = _section_order(text, language)
    assert order == sorted(order) and len(set(order)) == len(order), (
        f"the six frozen sections are not all present in order: {order}"
    )
    assert manifest["metrics_shown"], "metrics_shown is empty"
    assert manifest["contract_version"] == build_report.REPORT_CONTRACT_VERSION

    # --- and only then the absences ---
    for literal in LEAK_LITERALS:
        assert literal not in text, (
            f"the rendered report leaks the literal {literal!r} where a number "
            f"belongs"
        )
    for word in LEAK_WORDS:
        assert not re.search(rf"\b{re.escape(word)}\b", text, re.IGNORECASE), (
            f"the rendered report leaks the bare token {word!r}"
        )
    assert_no_invented_numbers(text, {"metrics.json": _metrics, "charts.json": _charts})


def test_gfm_cell_escaping_survives_a_pipe_backslash_and_newline(tmp_out: Path) -> None:
    """A hostile `label` cannot merge, split or shift a table cell.

    T-6-02, and reachable because `spec.json` is model-authored: a `|` in a label
    splits one cell into two and shifts every column after it, and a newline ends
    the row early and can inject a row the report never wrote. The measurement is
    the count of UNESCAPED separators, which is what a GFM renderer actually
    splits on - a correctly escaped `\\|` adds no separator and must not be
    counted as one.
    """
    hostile = "Польська | інтервальне\n голодування \\ тест"
    spec = json.loads((FIXTURES_DIR / "spec.example.json").read_text(encoding="utf-8"))
    spec["series"][0]["label"] = hostile
    spec_path = tmp_out / "spec.hostile.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    # Re-validate rather than assume: a derived spec the pipeline would refuse is
    # a broken test, and `common.validate_spec` is the pipeline's own verdict.
    assert common.validate_spec(spec) == [], "the derived hostile-label spec must be valid"

    document = json.loads((FIXTURES_DIR / "metrics.example.json").read_text(encoding="utf-8"))
    document["series"][0]["label"] = hostile
    _copy_chart_inputs(tmp_out)
    _write_metrics(tmp_out, document)
    assert make_charts.main(["--spec", str(spec_path), "--out", str(tmp_out)]) == 0
    assert _run_report(tmp_out, spec_path) == 0

    language = _spec_language()
    text = tmp_out.joinpath(build_report.REPORT_FILENAME).read_text(encoding="utf-8")
    header = _metrics_table_header(text, language)
    expected_separators = _unseparated_pipes(header)
    assert expected_separators > 0

    # A blank line precedes the block, so a stray line inside a cell cannot be
    # read as the end of the table and the rows after it as a new block.
    lines = text.splitlines()
    assert lines[lines.index(header) - 1] == "", (
        "the metrics table must be preceded by a blank line"
    )

    rows = _metrics_table_rows(text, language)
    assert len(rows) == len(document["series"]), (
        f"the hostile label changed the row count: {len(rows)} rows for "
        f"{len(document['series'])} series - a newline escaped its cell"
    )
    for row in rows:
        assert _unseparated_pipes(row) == expected_separators, (
            f"a row carries {_unseparated_pipes(row)} cell separators against the "
            f"header's {expected_separators}: {row!r}"
        )

    rendered_label = build_report.md_cell(hostile)
    assert rendered_label in rows[0], (
        f"the escaped label is not in its own row: {rendered_label!r}"
    )
    assert "\\|" in rows[0], "the raw pipe was not escaped as \\| in the rendered row"
    assert "\n" not in rendered_label, "md_cell must collapse a newline, not keep it"


def test_no_cross_series_rollup_appears_in_the_prose(tmp_out: Path) -> None:
    """No sentence ranks, counts or totals across series (D-03, T-6-09).

    The phrase list below is the one the plan names; the KEY-NAME vocabulary is
    reused from `test_contracts.ROLLUP_KEY_BLACKLIST` rather than a second list
    invented here, because a blacklist that exists in two places is a blacklist
    that will be updated in one of them. Each series' own row carries its own
    identity, so a per-series row is demonstrably a per-series row rather than an
    aggregate wearing a table's clothes.

    The row identity checked is the series' own `article` and `label`, the two
    values the report actually prints. `series_id` is deliberately NOT the
    discriminator: §8.6 freezes the table's identity to the label/project/article
    triple and `metrics_shown` addresses rows by INDEX, so printing `series_id`
    would be a report-content and manifest change this plan may not make.
    """
    from test_contracts import ROLLUP_KEY_BLACKLIST

    text, metrics, _charts, _manifest = _render_two_series(tmp_out)
    lowered = text.lower()

    for phrase in (
        "of 2 series",
        "both series are",
        "top series",
        "total across",
        "overall growth",
        "ranking",
        "of the 2 series",
    ):
        assert phrase not in lowered, (
            f"the prose carries the cross-series rollup phrase {phrase!r}"
        )
    for key in sorted(ROLLUP_KEY_BLACKLIST):
        assert not re.search(rf"\b{re.escape(key)}\b", lowered), (
            f"the document carries the D-03 rollup key name {key!r} as a claim"
        )

    rows = _metrics_table_rows(text, _spec_language())
    for index, node in enumerate(metrics["series"]):
        row = rows[index]
        assert build_report.md_cell(node["article"]) in row, (
            f"series[{index}]'s own row does not carry its own article, so it is "
            f"not demonstrably that series' row: {row!r}"
        )
        assert build_report.md_cell(node["label"]) in row, (
            f"series[{index}]'s own row does not carry its own label: {row!r}"
        )
        for other_index, other in enumerate(metrics["series"]):
            if other_index == index:
                continue
            assert other["article"] not in row, (
                f"series[{index}]'s row also carries series[{other_index}]'s article, "
                f"so a per-series row has stopped being a per-series row: {row!r}"
            )


def test_report_never_leaks_the_user_agent_or_a_cache_path(tmp_out: Path) -> None:
    """Neither output file names the transport, its contact or its cache.

    T-6-11. `report.md` is the artefact a reader is most likely to paste into a
    ticket, a forum post or a deck; a `User-Agent` contact string riding along in
    it publishes an address nobody asked to publish. The stage's only inputs are
    the three JSON documents, so nothing here should ever have a chance to appear.
    """
    _text, _metrics, _charts, _manifest = _render_two_series(tmp_out)
    outputs = {
        "report.md": tmp_out.joinpath(build_report.REPORT_FILENAME).read_text(encoding="utf-8"),
        "report.manifest.json": tmp_out.joinpath(
            build_report.REPORT_MANIFEST_FILENAME
        ).read_text(encoding="utf-8"),
    }
    forbidden = (
        common.DEFAULT_UA,
        "WTI_USER_AGENT",
        ".cache/",
        "user-agent",
        "User-Agent",
    )
    for name, payload in outputs.items():
        for needle in forbidden:
            assert needle not in payload, (
                f"{name} leaks {needle!r} - the report stage opens no connection "
                f"and has no transport detail to publish"
            )


def test_footer_states_source_as_of_what_was_measured_and_what_this_is_not(tmp_out: Path) -> None:
    """The named check for ROADMAP SC#3's footer clause and RPT-02's last prohibition.

    A report of pageviews that does not say what it is NOT is the failure this
    exists to catch: views are not buying intent, and a numeric projection here
    would be false confidence dressed as a finding. Four localized phrases are
    asserted by TOKEN LOOKUP rather than as literals, so a localization
    regression fails this test instead of quietly producing an English footer
    inside a Ukrainian report.

    The forecast vocabulary is matched against the document with the
    `not_a_forecast` phrase removed first. That phrase is the disclaimer and it
    contains the very word being prohibited ("прогноз" / "forecast"), so a naive
    substring search would fail a correct report and pass one that had dropped
    the disclaimer - the precise inversion the disclaimer exists to prevent. The
    disclaimer is asserted present in its own right, one line above.

    Paired with a non-empty document and a published manifest, so a builder that
    crashed into an empty file cannot pass an all-absence test.
    """
    text, metrics, _charts, manifest = _render_two_series(tmp_out)
    language = _spec_language()
    tokens = build_report.report_tokens(language)
    footer = _footer(text)

    # --- the positives ---
    assert text.strip(), "the rendered report is empty"
    assert footer.strip(), "the rendered report has an empty footer"
    assert manifest["contract_version"] == build_report.REPORT_CONTRACT_VERSION
    assert manifest["report_filename"] == build_report.REPORT_FILENAME

    # Read from the FOOTER, not from the whole document. `not_a_forecast` also
    # appears in «Наступний крок», so a whole-document check is satisfied by that
    # line alone and the footer could lose its own clause unnoticed - proven by
    # injected-defect probe 10, which deleted the footer's clause and left this
    # test green.
    for key in ("source", "measured", "not_a_forecast"):
        assert tokens[key] in footer, (
            f"the footer omits the {key!r} phrase {tokens[key]!r}; the footer reads "
            f"{footer!r}"
        )
    assert f"{tokens['as_of']} {metrics['as_of']}" in footer, (
        f"the footer must state the {tokens['as_of']!r} phrase beside the data's own "
        f"as_of value {metrics['as_of']!r}; the footer reads {footer!r}"
    )
    # A footer that says "as of" a different date than the data is the defect this
    # equality exists to catch, so the two are chained rather than checked once.
    assert manifest["as_of"] == metrics["as_of"], (
        f"the manifest's as_of {manifest['as_of']!r} differs from metrics.json's "
        f"{metrics['as_of']!r}"
    )
    assert f"{tokens['as_of']} {metrics['as_of']}" in _section_body(text, language, "metrics"), (
        "the as_of phrase must appear beside the value in the metrics section too, "
        "so a reader cannot meet two different dates in one document"
    )

    # --- and only then the absence, with the disclaimer itself removed ---
    residue = text
    for _ in range(4):
        replaced = residue.replace(tokens["not_a_forecast"], " ")
        if replaced == residue:
            break
        residue = replaced
    assert tokens["not_a_forecast"] not in residue, "the disclaimer phrase never appeared"
    for word in FORECAST_VOCABULARY:
        assert word not in residue.lower(), (
            f"the document projects {word!r} outside its own 'this is not' "
            f"disclaimer - pageviews are not a demand, revenue or willingness-to-pay "
            f"forecast"
        )


# --- 06-02 Task 3: the fail-closed matrix ------------------------------------


def _prepared_report_dir(tmp_path: Path, name: str) -> Path:
    """A directory holding a REAL chart-stage run: `series.csv`, `metrics.json`, `charts.json`.

    Built by running the actual chart stage rather than by writing two documents
    by hand, for the reason the tracer gives: a hand-written `charts.json` could
    drift from the emitter, and then the report stage would be proving its
    refusals against inputs the pipeline never produces. Every matrix row mutates
    a document in HERE, never a committed fixture.
    """
    out_dir = tmp_path / name
    out_dir.mkdir(parents=True, exist_ok=True)
    _copy_chart_inputs(out_dir)
    assert make_charts.main(
        ["--spec", str(FIXTURES_DIR / "spec.example.json"), "--out", str(out_dir)]
    ) == 0, "the chart stage must succeed before a refusal can be attributed to the report stage"
    return out_dir


def _patch_json(path: Path, mutate) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def _missing_metrics(out_dir: Path) -> None:
    out_dir.joinpath("metrics.json").unlink()


def _malformed_metrics(out_dir: Path) -> None:
    out_dir.joinpath("metrics.json").write_text('{"spec_name": "x", ', encoding="utf-8")


def _metrics_is_a_list(out_dir: Path) -> None:
    out_dir.joinpath("metrics.json").write_text("[1, 2, 3]", encoding="utf-8")


def _empty_series(out_dir: Path) -> None:
    _patch_json(out_dir.joinpath("metrics.json"), lambda d: d.__setitem__("series", []))


def _missing_confidence(out_dir: Path) -> None:
    def mutate(document):
        del document["series"][0]["confidence"]

    _patch_json(out_dir.joinpath("metrics.json"), mutate)


def _missing_trend_direction(out_dir: Path) -> None:
    def mutate(document):
        del document["series"][0]["trend_direction"]

    _patch_json(out_dir.joinpath("metrics.json"), mutate)


def _missing_period_days(out_dir: Path) -> None:
    def mutate(document):
        del document["series"][0]["period"]["days"]

    _patch_json(out_dir.joinpath("metrics.json"), mutate)


def _null_clean_pct_without_reason(out_dir: Path) -> None:
    def mutate(document):
        del document["series"][0]["growth"]["y2"]["clean"]["reason"]

    _patch_json(out_dir.joinpath("metrics.json"), mutate)


def _non_finite_clean_pct(out_dir: Path) -> None:
    path = out_dir.joinpath("metrics.json")
    # `Infinity` is what `json.dumps` emits for a non-finite float and what
    # `json.loads` reads back, so this is the shape a `1e400` in a real
    # `metrics.json` takes. The reused finite walk must catch it before anything
    # is formatted - a NaN drawn or printed is a silent lie.
    path.write_text(
        path.read_text(encoding="utf-8").replace('"pct": 16.7', '"pct": Infinity', 1),
        encoding="utf-8",
    )


def _missing_charts(out_dir: Path) -> None:
    out_dir.joinpath("charts.json").unlink()


def _wrong_charts_version(out_dir: Path) -> None:
    _patch_json(
        out_dir.joinpath("charts.json"),
        lambda d: d.__setitem__("contract_version", "charts.v0"),
    )


def _empty_charts(out_dir: Path) -> None:
    _patch_json(out_dir.joinpath("charts.json"), lambda d: d.__setitem__("charts", []))


def _shape_less_confidence_reasons(out_dir: Path) -> None:
    """`confidence_reasons` present but not a list of non-empty strings."""

    def mutate(document):
        document["series"][0]["confidence_reasons"] = [""]

    _patch_json(out_dir.joinpath("metrics.json"), mutate)


def _unmapped_confidence_reason(out_dir: Path) -> None:
    """A reason string no `analyze_trends` constant emits, in the DOCUMENT.

    The other refusal in this matrix (`test_reason_token_refuses_an_unmapped_reason`)
    calls `reason_token()` in process, so it proves the lookup refuses and nothing
    more. Only this row proves the STAGE's exit-1 path: that an unmapped reason
    stops the run with the English string named on stderr and NOTHING written. A
    refactor that caught `ReportError` and degraded to a printed warning would
    pass the in-process test and fail here - and would ship a half-translated
    Ukrainian report.
    """

    def mutate(document):
        document["series"][0]["confidence_reasons"] = [
            document["series"][0]["confidence_reasons"][0],
            "a reason no production code emits",
        ]

    _patch_json(out_dir.joinpath("metrics.json"), mutate)


# One entry per row of the matrix. `id` is the row's own name so a failure reads
# as a row name rather than as an index.
FAIL_CLOSED_ROWS = (
    pytest.param(_missing_metrics, id="metrics-absent"),
    pytest.param(_malformed_metrics, id="metrics-malformed"),
    pytest.param(_metrics_is_a_list, id="metrics-top-level-is-a-list"),
    pytest.param(_empty_series, id="metrics-series-empty"),
    pytest.param(_missing_confidence, id="metrics-confidence-absent"),
    pytest.param(_missing_trend_direction, id="metrics-trend-direction-absent"),
    pytest.param(_missing_period_days, id="metrics-period-days-absent"),
    pytest.param(_null_clean_pct_without_reason, id="null-clean-pct-without-reason"),
    pytest.param(_non_finite_clean_pct, id="metrics-non-finite-number"),
    pytest.param(_missing_charts, id="charts-absent"),
    pytest.param(_wrong_charts_version, id="charts-wrong-contract-version"),
    pytest.param(_empty_charts, id="charts-empty"),
    pytest.param(_unmapped_confidence_reason, id="metrics-unmapped-confidence-reason"),
)


@pytest.mark.parametrize("mutate", FAIL_CLOSED_ROWS)
def test_fail_closed_matrix_rows_refuse_with_exit_one_and_write_nothing(
    tmp_path: Path, capsys, mutate
) -> None:
    """Every malformed, missing, non-finite or untranslatable input is exit 1.

    §8.7's table has three outcomes and no fourth, so each row asserts all three
    halves of exit 1: the exit code itself, EXACTLY ONE `report failed:` line
    naming the condition (a model gets one line to act on, not a traceback and
    not two competing complaints), and neither output file written. A stage that
    failed halfway and left a manifest naming a report that is not there has told
    the reader something false, which is the defect the whole exit table exists
    to prevent.
    """
    before = _fixture_bytes()
    out_dir = _prepared_report_dir(tmp_path, "out.row")
    mutate(out_dir)

    assert _run_report(out_dir) == 1, (
        f"row {mutate.__name__!r}: a bad local input must be exit 1, never a "
        f"published report and never a traceback"
    )

    captured = capsys.readouterr()
    failures = [
        line for line in captured.err.splitlines() if line.startswith("report failed:")
    ]
    assert len(failures) == 1, (
        f"row {mutate.__name__!r}: expected exactly one 'report failed:' line, got "
        f"{len(failures)}: {captured.err!r}"
    )
    assert not out_dir.joinpath(build_report.REPORT_FILENAME).exists(), (
        f"row {mutate.__name__!r}: report.md was written on a failed run"
    )
    assert not out_dir.joinpath(build_report.REPORT_MANIFEST_FILENAME).exists(), (
        f"row {mutate.__name__!r}: report.manifest.json was written on a failed run"
    )
    assert _fixture_bytes() == before, "a matrix row mutated a committed fixture"


def test_fail_closed_missing_confidence_names_the_key_and_never_a_traceback(
    tmp_path: Path, capsys
) -> None:
    """The one row whose MESSAGE is the assertion: `series[0].confidence`.

    `require_display_fields` exists to turn a missing display field into a line a
    model can act on. Without it the same input produced a `KeyError` traceback
    halfway through a render - an output a model cannot use, and a document that
    may already be half-written. The assertion is on the message naming the exact
    `where.key`, plus the absence of a traceback, because a guard that refused
    correctly but named nothing would still leave the caller guessing.
    """
    out_dir = _prepared_report_dir(tmp_path, "out.keyname")
    _missing_confidence(out_dir)

    assert _run_report(out_dir) == 1
    err = capsys.readouterr().err
    assert "series[0].confidence" in err, (
        f"the failure must name the exact field to fix; stderr was {err!r}"
    )
    assert "KeyError" not in err, (
        f"a missing display field must be a refusal, not a traceback: {err!r}"
    )
    assert "Traceback" not in err, f"a refusal must not print a traceback: {err!r}"


# Each row: a mutation, and the exact `where.key` the refusal must name. Six
# fields, six keys - measured rather than narrated, because two injected-defect
# probes found that a refusal can be CORRECT and still name nothing:
#   probe 1: dropping `where.key` from the `confidence_reasons` message left the
#            whole matrix green (observed 18 passed), because no row asserted
#            that message;
#   probe 4: disabling the null-clean.pct reason guard left the matrix green too
#            (observed 18 passed), because the run then failed LATER and for a
#            different reason - `reason_token("uk", "None")` refuses on its own -
#            so a correct-looking exit 1 hid a missing guard.
# A guard that refuses for the wrong reason is not a guard.
KEY_NAMED_ROWS = (
    pytest.param(_missing_confidence, "series[0].confidence", id="confidence"),
    pytest.param(_missing_trend_direction, "series[0].trend_direction", id="trend-direction"),
    pytest.param(_missing_period_days, "series[0].period.days", id="period-days"),
    pytest.param(
        _null_clean_pct_without_reason, "series[0].growth.y2.clean", id="null-clean-reason"
    ),
    pytest.param(
        _shape_less_confidence_reasons,
        "series[0].confidence_reasons",
        id="confidence-reasons-shape",
    ),
)


@pytest.mark.parametrize("mutate,expected_key", KEY_NAMED_ROWS)
def test_fail_closed_refusal_names_the_exact_field_and_not_another(
    tmp_path: Path, capsys, mutate, expected_key
) -> None:
    """Every display-field refusal names ITS OWN `where.key`, and nothing else.

    Asserted per field rather than for one field, because a message naming the
    wrong key is as unusable as no message: a model reading
    `metrics.series[0].confidence` when the missing field was `period.days` would
    fix the wrong thing. The `not in` half is what catches a message that names
    the right key AND a wrong one.
    """
    others = [
        key
        for key in (
            "series[0].confidence",
            "series[0].trend_direction",
            "series[0].period.days",
            "series[0].growth.y2.clean",
            "series[0].confidence_reasons",
        )
        if key != expected_key
    ]
    out_dir = _prepared_report_dir(tmp_path, f"out.named.{expected_key.rsplit('.', 1)[-1]}")
    mutate(out_dir)

    assert _run_report(out_dir) == 1
    failures = [
        line for line in capsys.readouterr().err.splitlines() if line.startswith("report failed:")
    ]
    assert len(failures) == 1, f"expected one refusal line, got {failures!r}"
    assert expected_key in failures[0], (
        f"the refusal must name {expected_key!r}; it said {failures[0]!r}"
    )
    for other in others:
        # Identifier-boundary aware: `series[0].confidence` is a PREFIX of
        # `series[0].confidence_reasons`, so a plain substring test would report
        # the correct message for one field as naming another.
        assert not re.search(re.escape(other) + r"(?![A-Za-z0-9_])", failures[0]), (
            f"the refusal for {expected_key!r} also names {other!r}: {failures[0]!r}"
        )
    assert "Traceback" not in failures[0]


def test_invalid_spec_exits_with_exit_two_and_writes_nothing(tmp_path: Path, capsys) -> None:
    """The exit-2 row: a spec problem is exit 2, inherited, never intercepted.

    A different defect class from 06-01's `test_tracer_spec_problem_exits_two_and_writes_nothing`,
    which omits `series` entirely: this one is a spec that is a valid JSON object
    with the right root keys and an item carrying an UNKNOWN field and a missing
    `label`, which is the shape a hand-edited spec actually has. Both coexist on
    purpose - one row per exit code, each with its own input shape, because a
    single exit-2 assertion that passed for the wrong reason would be a gate with
    no reach.
    """
    spec = json.loads((FIXTURES_DIR / "spec.example.json").read_text(encoding="utf-8"))
    spec["series"][0]["unknown_field"] = "x"
    del spec["series"][0]["label"]
    spec_path = tmp_path / "spec.invalid.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")

    out_dir = _prepared_report_dir(tmp_path, "out.exit2")

    with pytest.raises(SystemExit) as exit_info:
        build_report.main(["--spec", str(spec_path), "--out", str(out_dir)])

    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "spec.json validation failed" in err, f"the spec header must be printed: {err!r}"
    assert "label" in err, f"the aggregated list must name the offending field: {err!r}"
    assert not out_dir.joinpath(build_report.REPORT_FILENAME).exists()
    assert not out_dir.joinpath(build_report.REPORT_MANIFEST_FILENAME).exists()


def test_report_tokens_are_required_per_language(tmp_path: Path, monkeypatch, capsys) -> None:
    """A language with no table, and a table missing one key, are the SAME defect.

    `test_charts.py::test_chart_tokens_are_required_per_language` established the
    shape for the sibling table; all three of its assertions are carried over here
    in meaning - exit 1, exactly one `report failed:` line naming the language,
    and nothing written.

    Two languages are exercised, for two different reasons:

    - `sv`, which no stage has a table for, as this plan specifies;
    - `ja`, which the CHART stage supports and the report stage does not. This is
      the genuinely reachable path, and the one §8.3 was written about: a `ja`
      spec produces a valid `charts.json` from the real chart stage and is then
      refused HERE. Testing only `sv` would exercise a path the real pipeline
      cannot reach, because the chart stage refuses `sv` first.
    """
    before = _fixture_bytes()
    out_dir = _prepared_report_dir(tmp_path, "out.language")

    for language in ("sv", "ja"):
        spec = json.loads((FIXTURES_DIR / "spec.example.json").read_text(encoding="utf-8"))
        spec["language"] = language
        spec_path = tmp_path / f"spec.{language}.json"
        spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")

        for name, mutate in (
            ("metrics.json", lambda d, lang=language: d.__setitem__("language", lang)),
            ("charts.json", lambda d, lang=language: d.__setitem__("language", lang)),
        ):
            path = out_dir.joinpath(name)
            document = json.loads(path.read_text(encoding="utf-8"))
            mutate(document)
            path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

        assert _run_report(out_dir, spec_path) == 1, (
            f"language {language!r} has no report token table and must be refused"
        )
        captured = capsys.readouterr()
        failures = [
            line for line in captured.err.splitlines() if line.startswith("report failed:")
        ]
        assert len(failures) == 1, (
            f"language {language!r}: expected exactly one 'report failed:' line, got "
            f"{failures!r}"
        )
        assert language in failures[0], (
            f"the refusal must name the language; stderr was {failures[0]!r}"
        )
        assert not out_dir.joinpath(build_report.REPORT_FILENAME).exists()
        assert not out_dir.joinpath(build_report.REPORT_MANIFEST_FILENAME).exists()

    # Every declared table is complete and every token is non-blank. A blank token
    # would render an empty cell or an empty heading and satisfy a membership
    # check, so membership is not enough on its own.
    for code, table in build_report.REPORT_TOKENS.items():
        assert set(table) == set(build_report.REQUIRED_REPORT_TOKENS), (
            f"REPORT_TOKENS[{code!r}] must define exactly the required keys, "
            f"differing on {sorted(set(table) ^ set(build_report.REQUIRED_REPORT_TOKENS))}"
        )
        for key, phrase in table.items():
            assert phrase.strip(), f"REPORT_TOKENS[{code!r}][{key!r}] is blank"

    # And the two states produce the IDENTICAL message, which is the property that
    # makes them the same defect rather than two similar ones.
    with pytest.raises(build_report.ReportError) as missing:
        build_report.report_tokens("sv")
    partial_table = {
        key: value
        for key, value in build_report.REPORT_TOKENS["uk"].items()
        if key != "not_computable"
    }
    monkeypatch.setattr(build_report, "REPORT_TOKENS", {**build_report.REPORT_TOKENS, "uk": partial_table})
    with pytest.raises(build_report.ReportError) as partial:
        build_report.report_tokens("uk")
    assert str(missing.value).replace("sv", "{language}") == str(partial.value).replace(
        "uk", "{language}"
    ), (
        f"a partial table and a missing one must report identically: "
        f"{missing.value!r} vs {partial.value!r}"
    )

    assert _fixture_bytes() == before, "the run mutated a committed fixture"


def test_reason_token_refuses_an_unmapped_reason() -> None:
    """No reason can fall back to English, and the refusal names it.

    Two halves. First, an unmapped reason raises and NAMES the reason verbatim,
    so the caller knows which upstream constant to add - which is also what makes
    an upstream RENAME loud instead of silent. Second, and structurally: the
    `uk` table's key set EQUALS the full set of `analyze_trends` constants whose
    name ends in `_REASON` or `_NOTE`, computed by walking `vars(analyze_trends)`.
    That equality is the form "no reason can fall back to English" actually takes:
    a reason constant added upstream without a localized phrase fails here, and a
    phrase left behind after the constant was deleted fails here too.
    """
    unmapped = "a reason no production code emits"
    with pytest.raises(build_report.ReportError) as refusal:
        build_report.reason_token("uk", unmapped)
    assert unmapped in str(refusal.value), (
        f"the refusal must name the reason verbatim; got {refusal.value!r}"
    )

    # The KEY SET is the set of the constants' VALUES, because `REASON_TOKENS` is
    # keyed by the verbatim English string (that is what makes an upstream RENAME
    # loud); the CONSTANT NAMES are what the `_REASON` / `_NOTE` suffix filter is
    # applied to.
    upstream = {
        value
        for name, value in vars(analyze_trends).items()
        if (name.endswith("_REASON") or name.endswith("_NOTE")) and isinstance(value, str)
    }
    assert upstream, (
        "no analyze_trends constant matched _REASON/_NOTE, so this test's structural "
        "half would silently pass on an empty set"
    )
    for code in build_report.REASON_TOKENS:
        assert set(build_report.REASON_TOKENS[code]) == upstream, (
            f"REASON_TOKENS[{code!r}] must cover every analyze_trends reason "
            f"constant, differing on {sorted(upstream ^ set(build_report.REASON_TOKENS[code]))}"
        )


# --- 06-03 Task 1: staleness is a DIGEST comparison, never a timestamp ---------
#
# RPT-03's claim, and the one place where the roadmap and the frozen contract
# disagree: ROADMAP SC#4 words staleness as "`metrics.json` is newer than the
# report", and §8.1 rejected that wording in the document. The tests below pin
# the mechanism the contract froze, and the second one makes the rejection
# mechanical rather than aspirational.

# The exact strings the AST-free source guard below looks for. They are the
# attribute names a file-modification-time check would reach for, and the
# module's own docstrings must not contain them either - so the prose in
# `report_is_stale` describes a file's modification time in WORDS and never
# quotes an identifier, or this test would fail on its own documentation.
TIMESTAMP_API_STRINGS = ("st_mtime", "getmtime", "st_ctime", "os.stat")


def test_staleness_is_decided_by_digest_not_by_timestamp(tmp_out: Path) -> None:
    """A consumer answers "is this report current?" from CONTENT, not from age.

    The decisive case is in the middle: `metrics.json` is re-serialized with
    identical values and different indentation, so every number the report would
    render is unchanged and only the BYTES differ. A digest comparison reports
    stale, which is correct - the manifest attests to specific bytes and those
    are not the bytes on disk. A timestamp comparison would say nothing at all
    here, and worse, would say "fresh" for a report rendered from entirely
    different content the moment that content was copied or checked out with a
    new mtime.
    """
    before = _fixture_bytes()
    _text, _metrics, _charts, _manifest = _render_two_series(tmp_out)

    metrics_path = tmp_out / "metrics.json"
    charts_path = tmp_out / "charts.json"
    original_metrics = metrics_path.read_bytes()
    original_charts = charts_path.read_bytes()

    assert not build_report.report_is_stale(tmp_out), (
        "a report rendered from exactly the documents on disk is not stale"
    )

    # The manifest describes the INPUTS, so report.md's own bytes cannot decide
    # the answer. A consumer holding a report it is about to overwrite is asking
    # whether the INPUTS moved, not whether the output is younger than itself.
    report_path = tmp_out / build_report.REPORT_FILENAME
    report_path.write_bytes(b"a different document entirely")
    assert not build_report.report_is_stale(tmp_out), (
        "report.md's own bytes decided the answer; the manifest's digests describe "
        "the inputs this report was rendered from, not the report itself"
    )

    # Same values, different bytes: the case a timestamp cannot see at all.
    document = json.loads(original_metrics.decode("utf-8"))
    reformatted = json.dumps(document, ensure_ascii=False, indent=4).encode("utf-8")
    assert reformatted != original_metrics, (
        "the re-serialization produced identical bytes, so the 'values equal, bytes "
        "differ' case this test exists for was not actually constructed"
    )
    metrics_path.write_bytes(reformatted)
    assert build_report.report_is_stale(tmp_out), (
        "metrics.json was rewritten with different bytes and the report was still "
        "called current; the digest comparison is what §8.1 froze"
    )

    # Restoring the exact bytes restores the answer - so the check is a function
    # of the content and of nothing else.
    metrics_path.write_bytes(original_metrics)
    assert not build_report.report_is_stale(tmp_out), (
        "the original metrics.json bytes were restored and the report is still "
        "reported stale, so something other than the digest is being compared"
    )

    # A missing input is stale, whichever of the two it is.
    metrics_path.unlink()
    assert build_report.report_is_stale(tmp_out), (
        "metrics.json is absent, so the report cannot describe what is on disk"
    )
    metrics_path.write_bytes(original_metrics)
    charts_path.unlink()
    assert build_report.report_is_stale(tmp_out), (
        "charts.json is absent, so the report cannot describe what is on disk"
    )
    charts_path.write_bytes(original_charts)

    # charts_sha256 is a live half of the check, not decoration: changing
    # charts.json alone must be enough.
    charts_document = json.loads(original_charts.decode("utf-8"))
    charts_path.write_text(
        json.dumps(charts_document, ensure_ascii=False, indent=4), encoding="utf-8"
    )
    assert build_report.report_is_stale(tmp_out), (
        "charts.json alone was rewritten and the report was still called current; "
        "charts_sha256 is not part of the decision"
    )

    assert _fixture_bytes() == before, "the run mutated a committed fixture"


def test_report_module_never_reads_a_timestamp() -> None:
    """The report module contains no file-timestamp API, and does compute a digest.

    Two halves, and the second is the load-bearing one. A module that simply
    never computed a digest would satisfy the first half perfectly, so the
    no-timestamp rule would pass on an implementation that had quietly stopped
    answering the question at all. Asserting both - the function exists, the
    digest is really taken, and no timestamp API appears anywhere in the source -
    is what makes the absence an enforced mechanism rather than a description.

    Substring matching is adequate here and the reasoning matters: a *parsed*
    walk would need to know which attribute is being read to call it a timestamp
    read, and a reader can also arrive at one through `Path.touch`, `copy2` or
    `shutil`. The source-level ban is the blunt instrument that cannot be
    defeated by any spelling, and it is exactly the claim the docstring makes.
    """
    source = _module_source()

    for forbidden in TIMESTAMP_API_STRINGS:
        assert forbidden not in source, (
            f"build_report's source contains {forbidden!r}. Staleness is a digest "
            "comparison (CONTRACTS.md 8.1): a file's modification time says when "
            "a file was touched, not which content was rendered, and it yields a "
            "false negative on any copy, checkout or restore"
        )

    assert "def report_is_stale(" in source, (
        "build_report has no report_is_stale seam, so the no-timestamp rule above "
        "is guarding nothing - a module that answers no question satisfies it"
    )
    assert "hashlib.sha256" in source, (
        "build_report computes no digest, so the absence of a timestamp API proves "
        "only that nothing is compared at all"
    )


# Each row destroys the manifest in one specific way, and each row asserts the
# answer in a False -> True -> False arc. The arc is the whole point: every
# branch here answers True, so a row that only asserted True would be satisfied
# by an implementation that returned True unconditionally - a staleness check
# that calls everything stale is not a staleness check.
UNUSABLE_MANIFEST_ROWS = (
    pytest.param(lambda p: p.unlink(), id="manifest-absent"),
    pytest.param(lambda p: p.write_bytes(b"{ not json"), id="manifest-malformed"),
    pytest.param(lambda p: p.write_bytes(b"[1, 2, 3]"), id="manifest-not-an-object"),
    pytest.param(
        lambda p: _drop_manifest_key(p, "metrics_sha256"), id="manifest-no-metrics-digest"
    ),
    pytest.param(
        lambda p: _drop_manifest_key(p, "charts_sha256"), id="manifest-no-charts-digest"
    ),
    pytest.param(
        lambda p: _drop_manifest_key(p, "metrics_sha256", value=17),
        id="manifest-digest-not-a-string",
    ),
)


def _drop_manifest_key(
    path: Path, key: str, *, value: object = "__DELETE__"
) -> None:
    """Delete one manifest key, or set it to a non-string, in place."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if value == "__DELETE__":
        document.pop(key, None)
    else:
        document[key] = value
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize("break_manifest", UNUSABLE_MANIFEST_ROWS)
def test_report_is_stale_answers_true_for_an_unusable_manifest(
    tmp_path: Path, break_manifest
) -> None:
    """A manifest that cannot answer the question is answered "stale", not raised.

    Five ways a manifest arrives unusable - absent, not JSON, not an object, and
    carrying either digest in a state that cannot be compared - plus a
    non-string digest, which is what a hand-edited or half-written manifest
    looks like. Every one of them means the manifest cannot be shown to describe
    the files beside it, and a consumer needs an answer in that case rather than
    an exception it would have to guess the meaning of.
    """
    out_dir = tmp_path / "out.unusable"
    out_dir.mkdir(parents=True, exist_ok=True)
    _render_two_series(out_dir)
    manifest_path = out_dir / build_report.REPORT_MANIFEST_FILENAME
    original = manifest_path.read_bytes()

    assert not build_report.report_is_stale(out_dir), (
        "a freshly published report is not stale - if this row cannot reach a "
        "False answer, the True below proves nothing"
    )

    break_manifest(manifest_path)

    assert build_report.report_is_stale(out_dir) is True, (
        f"row {break_manifest!r}: an unusable manifest must read as stale"
    )

    # ...and the same directory reads as current again once the manifest is whole,
    # so the answer moved because of the manifest and not because of a latch.
    manifest_path.write_bytes(original)
    assert build_report.report_is_stale(out_dir) is False, (
        f"row {break_manifest!r}: restoring the intact manifest did not restore the "
        "answer, so the check is not a function of the manifest's usability"
    )


# --- 06-03 Task 2: publication is atomic, and the manifest lands last ---------
#
# CONTRACTS.md 8.7's table has three outcomes and no fourth, and the exit-1 row
# promises TWO things about a failed publication: the prior bytes are preserved
# and no staging file is left behind. Neither half had a test. 06-02's
# injected-defect probe 6 found that fact by swallowing the manifest write's
# OSError and watching the whole fail-closed selection stay GREEN, which is the
# strongest possible statement that nothing covered it.


def test_atomic_publication_failure_preserves_prior_bytes_and_leaves_no_staging_file(
    tmp_out: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An interrupted `os.replace` replaces nothing and leaves nothing behind.

    The staging file plus one `os.replace` is the whole mechanism, so the
    failure at the replace is the one moment a naive implementation would leave
    either a truncated output or a stray `.report.md.*.tmp` in `--out`. Both
    halves are asserted against SENTINEL bytes: the prior report and the prior
    manifest must both survive byte for byte, which is a strictly stronger claim
    than "the file still parses".

    The test name keeps the substring `atomic` on purpose - 06-03's third
    non-vacuity probe selects it with `-k atomic` to prove it can go red, and a
    renamed test would silently make that probe select nothing.
    """
    _render_two_series(tmp_out)
    report_path = tmp_out / build_report.REPORT_FILENAME
    manifest_path = tmp_out / build_report.REPORT_MANIFEST_FILENAME
    report_path.write_bytes(b"sentinel-report")
    manifest_path.write_bytes(b"sentinel-manifest")

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("simulated interrupted replace")

    # The report module's own `os`, not `make_charts.common.os`: `dump_text` is
    # this module's own twin of `dump_json` and its `os.replace` is the call
    # under test.
    monkeypatch.setattr(build_report.os, "replace", fail_replace)

    assert _run_report(tmp_out) == 1, (
        "a publication failure is exit 1 under 8.7's table - never a silent "
        "success and never an uncaught traceback"
    )

    err = capsys.readouterr().err
    failures = [line for line in err.splitlines() if line.startswith("report failed:")]
    assert len(failures) == 1, (
        f"a model gets one line to act on, not a traceback and not two competing "
        f"complaints; got {failures!r}"
    )
    assert "could not write report output" in failures[0], (
        f"the refusal must name the failing publication step, so a reader can tell "
        f"a local input problem from a write failure; got {failures[0]!r}"
    )

    assert report_path.read_bytes() == b"sentinel-report", (
        "a failed publication must not touch the prior report's bytes"
    )
    assert manifest_path.read_bytes() == b"sentinel-manifest", (
        "a failed publication must not touch the prior manifest's bytes"
    )
    assert list(tmp_out.glob(".report.md.*.tmp")) == [], (
        "a failed publication must not leave a report staging file behind"
    )
    assert list(tmp_out.glob(".report.manifest.json.*.tmp")) == [], (
        "a failed publication must not leave a manifest staging file behind"
    )
    assert list(tmp_out.glob("*.tmp")) == [], (
        f"no staging file of any name may survive: "
        f"{[p.name for p in tmp_out.glob('*.tmp')]}"
    )


def test_manifest_is_published_only_after_the_report_is_in_place(
    tmp_out: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A report write that fails publishes NEITHER file.

    8.7's publication order, asserted rather than assumed: `report.md` is
    written first and the manifest only once it is in place, so a manifest never
    names a report that is not there. Breaking the report write is the direction
    that could publish a manifest alone, so it is the direction this drives.

    The directory is prepared by running the REAL chart stage only - no report
    has been published here yet, which is what lets the assertions below state
    that neither output file exists rather than merely that the prior bytes
    survived.
    """
    _copy_chart_inputs(tmp_out)
    assert make_charts.main(
        ["--spec", str(FIXTURES_DIR / "spec.example.json"), "--out", str(tmp_out)]
    ) == 0

    def fail_write(text: str, path: object) -> None:
        raise OSError("simulated interrupted report write")

    monkeypatch.setattr(build_report, "dump_text", fail_write)

    assert _run_report(tmp_out) == 1
    err = capsys.readouterr().err
    failures = [line for line in err.splitlines() if line.startswith("report failed:")]
    assert len(failures) == 1, f"expected one refusal line, got {failures!r}"
    assert not (tmp_out / build_report.REPORT_FILENAME).exists(), (
        "report.md was written even though its own write raised"
    )
    assert not (tmp_out / build_report.REPORT_MANIFEST_FILENAME).exists(), (
        "a manifest naming a report that was never written is the one thing 8.7's "
        "publication order exists to prevent, and it is on disk"
    )
    assert list(tmp_out.glob("*.tmp")) == []


def test_manifest_write_failure_keeps_the_prior_manifest_and_is_detectable_as_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The manifest write is the second half of the pair, and it is covered too.

    This is the case WINDOWS entry 23 records: 06-02's probe 6 swallowed the
    manifest write's `OSError` and the whole fail-closed selection stayed green,
    because the only publication test that existed patched the REPORT write.
    The other half of the pair had no coverage at all.

    The interesting property is not the exit code - it is that the resulting
    state is DETECTABLE. Here `metrics.json` genuinely changed first, so the new
    report is a render of new content while the prior manifest still describes
    the old bytes. That is a half-published directory, and the reason it is not
    a false statement about the data is Task 1's own seam: `report_is_stale`
    compares the manifest's recorded digest against the file on disk, finds
    them different, and says so. An mtime comparison would have said "fresh",
    which is precisely the defect RPT-03 was written to prevent.
    """
    out_dir = tmp_path / "out.manifest-failure"
    out_dir.mkdir(parents=True, exist_ok=True)
    _render_two_series(out_dir)

    manifest_path = out_dir / build_report.REPORT_MANIFEST_FILENAME
    report_path = out_dir / build_report.REPORT_FILENAME
    manifest_path.write_bytes(b"sentinel-manifest")

    # A genuine change to an input, so the prior manifest really is out of date
    # by the time its write fails.
    metrics_path = out_dir / "metrics.json"
    document = json.loads(metrics_path.read_text(encoding="utf-8"))
    metrics_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=4), encoding="utf-8"
    )

    def fail_dump(obj: object, path: object) -> None:
        raise OSError("simulated interrupted manifest write")

    monkeypatch.setattr(build_report, "dump_json", fail_dump)

    assert _run_report(out_dir) == 1
    err = capsys.readouterr().err
    failures = [line for line in err.splitlines() if line.startswith("report failed:")]
    assert len(failures) == 1, f"expected one refusal line, got {failures!r}"
    assert "could not write report output" in failures[0], (
        f"the refusal must name the failing publication step; got {failures[0]!r}"
    )

    assert manifest_path.read_bytes() == b"sentinel-manifest", (
        "a failed manifest write must leave the prior manifest's bytes untouched"
    )
    assert list(out_dir.glob("*.tmp")) == [], (
        f"no staging file may survive: {[p.name for p in out_dir.glob('*.tmp')]}"
    )

    # The half-published state is real - the report is the new render, the
    # manifest is the prior one - so it must be DETECTABLE, and it is.
    assert report_path.read_text(encoding="utf-8").strip(), (
        "the report was written before the manifest by design, so it is the new one"
    )
    assert build_report.report_is_stale(out_dir) is True, (
        "metrics.json changed and the manifest could not be republished, yet the "
        "directory reports itself current - a consumer would present a report as "
        "describing input it no longer describes"
    )


