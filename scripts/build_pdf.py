"""Render metrics.json + charts.json into the PDF document's body. Wave 2 of Phase 9.

The fifth delivery format, and the ONLY one whose layout differs from `report.md`: an
11-column metrics table cannot fit A4 (17.5 cm of usable width against eleven cells), so
the PDF replaces it with one **card per series**. Everything else is the same report.

    0   planned body rendered, nothing written      (main() is plan 09-05)
    1   a local input, toolchain, or integrity failure
    2   an invalid spec.json

**This half is PURE.** No I/O, no `pypandoc`, no `typst`, no `subprocess` — in
`build_report.render_report`'s own shape, so a test reads a value out of the returned
`PdfDocument` instead of scraping a rendered page, and so T1-T7 are green on a machine
where the PDF toolchain is not installed at all. The render call and the manifest append
are plan 09-05; the typst preamble, the font probe and the image-path recipe are 09-04.

Three rules this module does not have a private answer to, because it is a READER:

* **ANAL-06 / the never-recompute rule.** Every number is read from `metrics[...]` or
  `charts[...]` and formatted. Nothing is derived. The chart stage's own counts
  (`gaps`, `anomalies_drawn`, `log_masked_points`) are READ, never recomputed — the same
  discipline `build_report` follows, and the reason `charts.v1` §7.3's "a consumer
  inherits this rule" sentence exists.
* **The null rule.** `growth_phrase` is IMPORTED from `build_report`, not reimplemented.
  A `null` growth is the localized not-computable token plus that window's own reason —
  never `0`, never `0.0`, never an em-dash, and **never an empty cell**, which in a
  bordered card reads as "measured, result zero".
* **The document's own words.** `report_tokens`, `reason_token`, `confidence_token`,
  `trend_token` and `make_charts.chart_tokens` are IMPORTED. A language the report stage
  refuses is refused here by the same function with the same message, so the PDF cannot
  become a second entrance for a script `report.md` will not write — the defect class
  FINDING-05 was.

PARITY is the contract, and it is mechanical: `PdfDocument.shown` must equal the report
manifest's `metrics_shown` as a SET. Not a subset. Fewer would be a lossy rendering of a
document whose whole purpose is to stand in for the Markdown; more would be a number no
consumer's manifest attests to. That single equality is the TZ's «паритет» made checkable.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import unquote

import analyze_trends
import build_report
import make_charts
from build_report import (
    REPORT_V1_FORMATS,
    RATIFIED_PDF_FORMAT,
    RATIFIED_V1_FORMATS,
    ReportError,
    assert_formats_contract,
    confidence_token,
    format_number,
    growth_phrase,
    md_cell,
    reason_token,
    report_tokens,
    trend_token,
)
from common import dump_json, load_and_validate_spec, log, setup_logging

# The bare relative name, for the same reason `build_report.REPORT_FILENAME` is one: a
# consumer resolves it against the directory it was given, and CONTRACTS.md 8.1.1
# clause 2 froze this exact object as the one appended entry.
PDF_FILENAME = "report.pdf"

#: The committed typst template, beside this module. A package-relative path, NOT a
#: `__file__`-relative guess and not a working-directory assumption: pandoc resolves a
#: template path against its own process, and a template it cannot find fails with
#: `Could not find data file 'templates/...'` naming a directory the caller never chose.
PREAMBLE_PATH = Path(__file__).resolve().parent / "wti-preamble.typ"

#: Candidate font families, in preference order, per document language. Every candidate
#: is a widely-installed sans-serif that covers BOTH Latin and Cyrillic, so one list serves
#: `en` and `uk` and the choice is about availability rather than about coverage.
#:
#: 09-01 measured that the assignment's mandated stack — "Arial", "Liberation Sans",
#: "DejaVu Sans" — has TWO of its three families absent on a stock Windows machine,
#: because matplotlib ships `DejaVuSans.ttf` inside its own package rather than in the
#: font registry typst reads. And typst does not fail on an unknown family: it warns and
#: substitutes. So the family is PROBED, never assumed, and a missing one is a refusal
#: rather than a silent substitution.
FONT_CANDIDATES: tuple[str, ...] = (
    "Arial",
    "Liberation Sans",
    "DejaVu Sans",
    "Noto Sans",
    "Segoe UI",
    "FreeSans",
)

#: The signal pypandoc emits when it cannot fetch an image, through the `pypandoc`
#: LOGGER. Measured text:
#:
#:     WARNING pypandoc: Could not fetch resource chart_overlay.png: replacing image with description
#:
#: This is the most dangerous behaviour in the whole phase — a complete, correct-looking
#: report with charts silently replaced by their alt text, and exit code 0 — and it is why
#: the render is not allowed to report its own success. `charts.v1` §7.4 exists to stop a
#: picture dropping data without saying so; this is that defect arriving through the
#: delivery format instead of through the chart stage.
#:
#: ROUTING, which cost two wrong implementations to learn. The message reaches Python
#: through the `logging` module, NOT through `sys.stderr` — so `contextlib.redirect_stderr`
#: around the call captures **zero bytes** of it, and a first version of this stage claimed
#: a stderr check it did not have. `_LogCapture` below attaches to the logger instead.
#: Because the message names the FILE, this signal is strictly more informative than the
#: marker count: it says which chart went missing, not only how many did.
UNFETCHABLE_RESOURCE = "Could not fetch resource"

#: Byte pattern marking an image XObject in the produced PDF.
#:
#: MEASURED, and the measurement is the whole content of this constant. Driving the real
#: converter with charts deliberately missing from the output directory:
#:
#:     5 charts present  -> 10 occurrences
#:     4 charts present  ->  8 occurrences
#:     1 chart  present  ->  2 occurrences
#:
#: So the factor is exactly 2 - typst writes each image once in a page's resource
#: dictionary and once in the object itself. The render REQUIRES the equality rather than
#: a lower bound, because a lower bound cannot catch a single missing chart: 4 of 5 charts
#: still yields 8 markers, which clears any `>= 5`. And the equality fails CLOSED - if a
#: future typst changes its object layout, the render refuses with a message saying the
#: measured factor has moved and the gate needs re-measuring, instead of silently
#: accepting a document that has lost its evidence.
IMAGE_XOBJECT = b"/Subtype/Image"
IMAGE_MARKERS_PER_IMAGE = 2

#: The signal pandoc emits when it cannot fetch an image. Kept as a DEFENCE, and
#: explicitly NOT counted as a gate: measured, `pypandoc.convert_text` does not forward
#: pandoc's diagnostics to the calling process's stderr at all — a run with four of five
#: charts deleted captured ZERO bytes of stderr. So an earlier version of this stage
#: claimed "two independent post-render checks" and had one. The warning text is still
#: matched, because it costs nothing and would become the primary signal if pypandoc ever
#: starts forwarding it; what changed is the claim, not the code.
UNFETCHABLE_RESOURCE = "Could not fetch resource"


class PdfError(RuntimeError):
    """A local input, toolchain, or render failure the model can act on.

    Distinct from `build_report.ReportError` (a bad input document) because the two call
    for different actions: a `ReportError` says "the data is wrong", a `PdfError` says
    "the toolchain is missing or the render did not produce what it promised". Both are
    exit 1 to the caller; the MESSAGE is what the model reads.
    """


@dataclass(frozen=True)
class RenderedPdf:
    """What one successful render produced, for the caller to publish."""

    path: Path
    page_count: int
    font_family: str
    bytes_written: int

#: The window labels the card shows, in the order the report shows them. These are
#: NAMES: `render_report` labels its windows the same way and the fidelity tokeniser
#: exempts a digit run followed by a letter for exactly this reason. The labels are
#: frozen against `metrics.v1`'s own window names, not invented here.
WINDOW_LABELS: tuple[tuple[str, str], ...] = (("m3", "3M"), ("y1", "1Y"), ("y2", "2Y"))

#: Where the user is told to get `typst`, named in every refusal that concerns it. The
#: assignment's headline risk is that a PDF toolchain is absent from an agent runtime, and
#: the correct response to that is an actionable line, not a traceback.
TYPST_INSTALL = "winget install Typst.Typst --silent --accept-package-agreements"
PDF_DEPS_INSTALL = "pip install -r requirements-pdf.txt"


@dataclass(frozen=True)
class PdfDocument:
    """What `render_pdf_document` plans, before anything is written or rendered.

    `body` is pandoc-flavoured Markdown — grid tables, image references, emphasis —
    because the ratified engine is `pandoc --pdf-engine=typst` (09-ENGINE-DECISION.md).
    The other four fields are what a test asserts instead of scraping text.
    """

    body: str
    shown: tuple[str, ...]
    images: tuple[str, ...]
    language: str
    as_of: str
    spec_name: str


def display_article(article: str) -> str:
    """The article title as a READER should see it: percent-decoded, once, for display.

    `metrics.json` stores `article` percent-ENCODED, and that is deliberate and frozen:
    Phase 8 recorded that the stored slug stays encoded because it is the resolver's AQS
    cache key and `fetch_pageviews.series_url` interpolates it unquoted. Nothing here
    changes that — this is a **presentation** transform, applied on the way into the
    document and never on the way to disk.

    The owner's visual pass of the 09-01 spike found the encoded form
    (`%D0%91%D1%96%D0%B7%D0%BD%D0%B5%D1%81...`) unreadable in a card row labelled
    `Стаття`, which is the whole point of the card. `unquote` is a lossless INVERSE of
    the encoding for well-formed input, so this introduces no number (ANAL-06 untouched),
    is not one of the localized row strings, and is therefore outside the parity
    requirement the manifest's `metrics_shown` encodes.

    `unquote` leaves an invalid escape alone instead of raising, which is what makes this
    safe to call on any stored slug: a title carrying a literal `%` comes back unchanged
    rather than becoming a crash inside a stage that must never leave a half-written
    document on disk. `test_display_article_leaves_a_malformed_escape_alone` pins that.
    """
    return unquote(article)


def _card(title: str, rows: Sequence[tuple[str, str]]) -> list[str]:
    """One series as a pandoc GRID table, in place of the 11-column metrics table.

    The grid form (`+---+` rules, `**bold**` header, `+===+` after the header row,
    equal column widths in every row) is the canon carried over from the external
    `cv-render` project, where a table that "flooded" is a documented failure. Every
    rule here has a reason in that history and is carried with the rule.

    The label column is padded to the WIDEST label rather than to a constant, so a long
    localized label widens the column instead of forcing every value onto a second line —
    the specific defect an 11-column table has on A4.
    """
    label_width = max((len(label) for label, _ in rows), default=0)
    value_width = max((len(value) for _, value in rows), default=0)
    rule = "+" + "-" * (label_width + 2) + "+" + "-" * (value_width + 2) + "+"
    out = [f"**{md_cell(title)}**", "", rule]
    for label, value in rows:
        out.append(
            f"| **{md_cell(label)}**{' ' * (label_width - len(label))} "
            f"| {md_cell(value)} |"
        )
        out.append(rule)
    out.append("")
    return out


def _image_reference(filename: str) -> str:
    """One chart, referenced by its BARE name and resolved by the caller's run directory.

    The path is deliberately NOT absolute here. 09-01's spike measured that typst
    REJECTS an absolute Windows path with *path contains invalid component `"C:"`*
    whenever `--root` is set, and the `--root`-set configuration is the one a
    security-minded implementer chooses. Pandoc resolves a bare name against the working
    directory it is given, which 09-05 controls; 09-04 pins the recipe with a test that
    runs the real conversion with a root set, because that is the configuration nobody
    tests by hand and the one where every chart silently disappears.
    """
    return f"![]({filename}){{width=100%}}\n"


def render_pdf_document(
    spec: Mapping[str, Any],
    metrics: Mapping[str, Any],
    charts: Mapping[str, Any],
) -> PdfDocument:
    """The pure planning half: the body, the shown pointers, the image inventory.

    No I/O happens here. The three return fields are the whole contract surface a test
    needs: `shown` is what makes "the PDF shows exactly the numbers the Markdown shows" a
    set comparison rather than a review habit, and `images` is what makes the `2N+1`
    inventory checkable without opening the document.

    `spec` is read for exactly one thing — the document language, which the report stage
    also takes from the spec and cross-checks against `metrics.json`. The PDF does not
    read `report.md` at all: that file is a Markdown-specific rendering of an 11-column
    table, and parsing it back into structure is extracting numbers off a page, which is
    what ANAL-06 forbids. The PDF reads the same two documents `build_report` reads.
    """
    language = str(metrics.get("language") or spec.get("language"))
    if spec.get("language") != language:
        raise ReportError(
            f"metrics language {language!r} does not match spec language "
            f"{spec.get('language')!r}; the PDF stage writes one document language"
        )
    # The refusal happens here, before a single character of the body exists, and it is
    # `build_report`'s own lookup — so a language the report stage refuses is refused here
    # identically, and the PDF cannot become a second entrance for it.
    tokens = report_tokens(language)
    series_nodes = metrics["series"]
    chart_entries = charts["charts"]
    shown: list[str] = []

    def show(pointer: str) -> None:
        """Record one rendered metric path, first occurrence wins.

        The same dedup discipline `render_report` documents, for the same reason: the
        same value is legitimately displayed more than once (a percentage in «Висновок»
        and again in the card), and a list with duplicates would make a consumer's set
        comparison depend on how many sections happened to quote it.
        """
        if pointer not in shown:
            shown.append(pointer)

    lines: list[str] = []

    # --- the document's identity line, before any section ------------------
    lines.append(f"# {tokens['title']}")
    lines.append("")
    lines.append(f"*{tokens['as_of']} {metrics['as_of']} · {metrics['spec_name']}*")
    lines.append("")

    # --- the six sections, in the frozen order, WALKED not retyped ----------
    for section in build_report.SECTION_TOKEN_KEYS:
        lines.append(f"## {tokens[section]}")
        lines.append("")

        if section == "conclusion":
            for index, node in enumerate(series_nodes):
                prefix = f"series[{index}]"
                show(f"{prefix}.trend_direction")
                show(f"{prefix}.confidence")
                show(f"{prefix}.avg_daily_views")
                for window, _label in WINDOW_LABELS:
                    show(f"{prefix}.growth.{window}.clean.pct")
                lines.append(
                    f"- {md_cell(node['label'])}: "
                    f"{trend_token(language, str(node['trend_direction']))} — "
                    + ", ".join(
                        f"{label} {growth_phrase(tokens, language, node['growth'][window])}"
                        for window, label in WINDOW_LABELS
                        if node["growth"][window].get("clean", {}).get("pct") is not None
                    )
                    + f" {tokens['volume_base']} {format_number(node['avg_daily_views'])}"
                    f"/d, {tokens['confidence']}: "
                    f"{confidence_token(language, str(node['confidence']))}"
                )
                lines.append("")

        elif section == "metrics":
            # THE layout change: one card per series, replacing the 11-column table.
            for index, node in enumerate(series_nodes):
                prefix = f"series[{index}]"
                for pointer in (
                    "label", "project", "article", "language", "period.start",
                    "period.end", "period.days", "total_views", "avg_daily_views",
                    "trend_direction", "confidence", "anomaly_share",
                    "confidence_reasons",
                ):
                    show(f"{prefix}.{pointer}")
                for window, _label in WINDOW_LABELS:
                    show(f"{prefix}.growth.{window}.clean.pct")

                rows: list[tuple[str, str]] = [
                    (tokens["project"], md_cell(node["project"])),
                    (tokens["article"], md_cell(display_article(str(node["article"])))),
                    (tokens["language"], md_cell(node["language"])),
                    (
                        tokens["period"],
                        f"{node['period']['start']}..{node['period']['end']} "
                        f"({format_number(node['period']['days'])})",
                    ),
                    (tokens["total_views"], format_number(node["total_views"])),
                    (tokens["avg_daily_views"], format_number(node["avg_daily_views"])),
                    (tokens["direction"], trend_token(language, str(node["trend_direction"]))),
                    (tokens["confidence"], confidence_token(language, str(node["confidence"]))),
                    (tokens["anomaly_share"], format_number(node["anomaly_share"])),
                ]
                for window, label in WINDOW_LABELS:
                    rows.append(
                        (
                            f"{tokens['growth']} {label} {tokens['window']}",
                            growth_phrase(tokens, language, node["growth"][window]),
                        )
                    )
                lines.extend(_card(md_cell(node["label"]), rows))

        elif section == "trust":
            for index, node in enumerate(series_nodes):
                show(f"series[{index}].confidence")
                show(f"series[{index}].confidence_reasons")
                lines.append(
                    f"- {md_cell(node['label'])} — "
                    f"{confidence_token(language, str(node['confidence']))}: "
                    + "; ".join(
                        md_cell(reason_token(language, str(reason)))
                        for reason in node["confidence_reasons"]
                    )
                )
                lines.append("")

        elif section == "charts":
            # READ the chart manifest's own counts. Never recompute a total it publishes
            # (build_report's own note on this: deriving `gaps_days` from `gaps` is an
            # open §8.4 question that stage is forbidden to answer, and this one is not
            # the stage that may answer it either).
            for entry in chart_entries:
                lines.append(_image_reference(str(entry["filename"])))
                lines.append(
                    f"_{md_cell(entry['label'])} "
                    f"[{md_cell(entry['filename'])}] — "
                    f"{tokens['disclosure_gaps']}: {format_number(sum(gap['days'] for gap in entry['gaps']))}; "
                    f"{tokens['disclosure_anomalies_drawn']}: {format_number(entry['anomalies_drawn'])}; "
                    f"{tokens['disclosure_log_masked_points']}: {format_number(entry['log_masked_points'])}"
                    + "_"
                )
                lines.append("")

        elif section == "limitations":
            for index, node in enumerate(series_nodes):
                lines.append(
                    f"- {md_cell(node['label'])}: "
                    f"{md_cell(reason_token(language, str(node['seasonality']['note'])))}"
                )
            for assumption in spec.get("assumptions") or []:
                lines.append(f"- {tokens['assumption']}: {md_cell(assumption)}")
            lines.append(
                f"- {tokens['source']}: Wikimedia AQS Pageviews · "
                f"{tokens['measured']}: daily all-access all-agent pageviews."
            )
            lines.append("")

        elif section == "next_step":
            # A fixed non-numeric localized token. REC-01's ranking is explicitly NOT
            # built (v2), exactly as in the Markdown report.
            lines.append(tokens["next_step"])
            lines.append("")

    # --- the mandatory closing line, a frozen part of the report contract ---
    lines.append(f"**{tokens['not_a_forecast']}.**")

    return PdfDocument(
        body="\n".join(lines) + "\n",
        shown=tuple(shown),
        images=tuple(str(entry["filename"]) for entry in chart_entries),
        language=language,
        as_of=str(metrics["as_of"]),
        spec_name=str(metrics["spec_name"]),
    )


# ============================================================ the renderer (wave 3)


def typst_executable() -> str:
    """The `typst` binary, or a refusal naming the install command.

    `shutil.which` first, then the two WinGet locations the assignment's external
    reference used. The fallback is not decoration: on this machine `typst` IS on PATH
    while it is NOT in `WinGet\\Links`, so a PATH-only lookup happens to work here and
    would fail on a machine where WinGet put it only in `Packages`. Both are searched, and
    the refusal names the command rather than raising `FileNotFoundError`.
    """
    found = shutil.which("typst")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        packages = Path(local) / "Microsoft" / "WinGet" / "Packages"
        if packages.is_dir():
            for candidate in sorted(packages.glob("Typst.Typst_*")):
                for binary in candidate.rglob("typst.exe"):
                    return str(binary)
    raise PdfError(
        f"build_pdf: the typst binary was not found on PATH or under "
        f"%LOCALAPPDATA%\\Microsoft\\WinGet\\Packages. Install it with: {TYPST_INSTALL}"
    )


def installed_font_families() -> frozenset[str]:
    """Every family name `typst` can see, or a refusal.

    A PROBE, not an assumption. Two facts make it necessary, both measured in 09-01:

    * typst resolves families from the OS font registry, and matplotlib ships
      `DejaVuSans.ttf` INSIDE its package — so the assignment's claim that "DejaVu arrives
      with matplotlib, therefore Cyrillic can never disappear" is false. `typst fonts` on a
      stock machine lists `DejaVu Sans Mono` and no `DejaVu Sans`.
    * typst does not FAIL on an unknown family. It prints `warning: unknown font family`
      and substitutes its own default. A warn-and-substitute ships a document with no
      Cyrillic and no error, which is the one outcome this project cannot have — it is
      FINDING-05's shape (a language defect no test of the arithmetic would catch) arriving
      through the font stack instead of through a token table.
    """
    binary = typst_executable()
    proc = subprocess.run(
        [binary, "fonts"], capture_output=True, text=True, encoding="utf-8", check=False,
    )
    if proc.returncode != 0:
        raise PdfError(
            f"build_pdf: `typst fonts` failed with exit {proc.returncode}: "
            f"{proc.stderr.strip()[:300] or 'no diagnostic'}"
        )
    return frozenset(line.strip() for line in proc.stdout.splitlines() if line.strip())


def probe_font_family(language: str) -> str:
    """One font family typst can actually see, for `language`'s script.

    Fail-closed: when no candidate is installed the answer is a `PdfError` naming the
    install command, never a family name we hope resolves. The accepted set is matched by
    **EXACT line equality** against `typst fonts` — a substring test is how this plan's
    author briefly recorded `DejaVu Sans` as present on a machine that has only
    `DejaVu Sans Mono`, and a font gate satisfied by a near-miss is not a gate.

    What this does NOT claim: that the family covers the script. Coverage is observable
    only in the rendered text, so it is proven by the integration test that extracts the
    Ukrainian strings back out of the PDF, and by a human opening the document. The
    contract here is the narrower, honest one — *this family exists to typst*.
    """
    available = installed_font_families()
    for family in FONT_CANDIDATES:
        if family in available:
            return family
    raise PdfError(
        f"build_pdf: none of the candidate font families is installed for typst "
        f"({', '.join(FONT_CANDIDATES)}); typst would warn and silently substitute, and a "
        f"substituted font may have no {language!r} glyphs. Install one of them, or install "
        f"typst itself with: {TYPST_INSTALL}"
    )


def _metadata_yaml(spec_name: str, as_of: str, font_family: str, language: str) -> str:
    """The values the preamble reads, as pandoc metadata.

    The assignment's claim that the margins reach the template only via `--metadata-file`
    and not via `-V` is about the *external* template's `conf` function. What is measured
    here is simpler and is what the preamble relies on: pandoc's own `$var$` substitution
    fills the placeholders, and the assignment's `sys.inputs` route does NOT receive
    pandoc metadata at all. Values are quoted because a `spec_name` is model-authored and
    may contain a colon, a hash or a quote — an unquoted YAML scalar would turn a legitimate
    spec name into a parse error, or worse, into a different value.
    """
    def quoted(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    return (
        f"spec-name: {quoted(spec_name)}\n"
        f"as-of: {quoted(as_of)}\n"
        f"font-family: {quoted(font_family)}\n"
        f"lang: {quoted(language)}\n"
    )


def render_pdf(
    body: str,
    *,
    out_dir: str | Path,
    spec_name: str,
    as_of: str,
    language: str,
    images: Sequence[str],
) -> RenderedPdf:
    """Render `body` to `out_dir/report.pdf`, and PROVE the charts survived.

    The recipe is measured, not assumed (09-01 / this plan's own probe):

    * image references are **bare names** plus `--resource-path=<out_dir>`. An absolute
      Windows path is what 09-01's spike used and it works ONLY because pypandoc sets no
      root; the moment a `--root` is set, typst rejects the drive letter outright. And a
      bare name with NO `--resource-path` is worse than either: pypandoc logs
      `Could not fetch resource`, the conversion exits **0**, and the result is a complete
      report with that chart replaced by its description.
    * **no `--root`.** Setting it is not merely unnecessary — measured, it turns a working
      recipe into `source file must be contained in project root`. The configuration a
      security-minded implementer reaches for is the one that breaks the naive path, and
      the recipe here is chosen to be root-INDEPENDENT instead of to be patched per root.

    TWO post-render checks, and they are genuinely independent because they fail for
    different reasons:

    1. **The logger signal.** `_LogCapture` collects the `pypandoc` logger's records during
       the conversion and the render refuses if any names an unfetchable resource. It says
       WHICH chart went missing, and it fires even when the count is coincidentally right.
    2. **The marker count**, computed from bytes so the guarantee survives on a machine
       without `pypdf` — which is every user's machine, since `pypdf` is a dev dependency.
       It is required to be EXACT (`IMAGE_MARKERS_PER_IMAGE` per referenced chart, a
       measured property of typst's object layout), so a document missing a single chart is
       refused rather than published, and a change in that layout fails CLOSED with a
       message saying the gate needs re-measuring.

    An earlier draft claimed two signals and had one, because it looked for the warning on
    `sys.stderr` where it never appears. Both are here now, each demonstrated to fire.

    Publication is atomic: a temporary file in the same directory, then `os.replace`. A
    failed render removes the temporary file and leaves any previous `report.pdf`
    byte-identical, so a reader never finds a half-written document.
    """
    # Lazy import, and the reason is a test failure rather than a style preference:
    # `pypandoc` is absent from the phase-local .venv by default, so a module-level import
    # would break COLLECTION for every test that imports this module — which is every test
    # in test_build_pdf.py. 09-03's purity test enforces the laziness at the AST level.
    try:
        import pypandoc
    except ImportError as exc:  # pragma: no cover - exercised by 09-05's exit-code table
        raise PdfError(
            f"build_pdf: pypandoc is not installed. Install the optional PDF "
            f"dependencies with: {PDF_DEPS_INSTALL}"
        ) from exc

    directory = Path(out_dir)
    if not PREAMBLE_PATH.is_file():
        raise PdfError(f"build_pdf: the typst preamble is missing: {PREAMBLE_PATH}")

    font_family = probe_font_family(language)
    metadata = directory / "report.pdf.meta.yaml"
    target = directory / PDF_FILENAME
    # The staging name MUST end in `.pdf`: pypandoc validates the output extension and
    # refuses anything else with "PDF output needs an outputfile with '.pdf' as a
    # fileending" — a refusal that is correct and that a `.partial` suffix would earn on
    # every single run. So the temporary carries the extension and the dot-prefix, and
    # `os.replace` is what actually publishes it.
    staged = directory / f".{PDF_FILENAME.removesuffix('.pdf')}.partial.pdf"
    metadata.write_text(
        _metadata_yaml(spec_name, as_of, font_family, language), encoding="utf-8"
    )

    extra_args = [
        "--pdf-engine=typst",
        f"--template={PREAMBLE_PATH}",
        f"--metadata-file={metadata}",
        f"--resource-path={directory}",
        "--standalone",
    ]
    try:
        # pypandoc reports through the `logging` module, not `sys.stderr`, so the
        # unfetchable-resource signal is collected with a handler on its logger. The
        # redirect below is kept as well, for the diagnostics a hard failure prints; it
        # contributes nothing to the image gate, and the docstring says so.
        capture = _LogCapture()
        captured = io.StringIO()
        with capture, contextlib.redirect_stderr(captured):
            pypandoc.convert_text(
                body, "pdf", format="md", outputfile=str(staged), extra_args=extra_args
            )
        diagnostics = captured.getvalue()
        if not staged.is_file() or staged.stat().st_size == 0:
            raise PdfError(
                "build_pdf: the converter reported success but wrote no PDF; the typst "
                f"preamble may have failed to compile. Diagnostics: "
                f"{capture.text or diagnostics.strip()[:400] or 'none'}"
            )
        unfetchable = [line for line in capture.lines if UNFETCHABLE_RESOURCE in line]
        if unfetchable:
            raise PdfError(
                f"build_pdf: the converter could not fetch "
                f"{len(unfetchable)} image(s) and replaced each with its description, so "
                f"the document would open and look complete while missing its evidence: "
                f"{unfetchable[0].strip()[:300]}. Refused rather than published. Check that "
                f"every name in {list(images)!r} exists in {str(directory)!r}."
            )
        markers = staged.read_bytes().count(IMAGE_XOBJECT)
        expected = IMAGE_MARKERS_PER_IMAGE * len(images)
        if markers != expected:
            raise PdfError(
                f"build_pdf: the produced PDF carries {markers} image markers where "
                f"{expected} were expected for {len(images)} chart(s) "
                f"({IMAGE_MARKERS_PER_IMAGE} per chart, measured). Either a chart was "
                f"dropped somewhere between the body and the renderer, or typst's object "
                f"layout has changed and this gate needs re-measuring. Refused rather "
                f"than published. Check that every name in {list(images)!r} exists in "
                f"{str(directory)!r}."
            )
        os.replace(staged, target)
    except PdfError:
        _discard(staged)
        raise
    except Exception as exc:  # noqa: BLE001 - the toolchain's own failure modes
        _discard(staged)
        raise PdfError(
            f"build_pdf: the PDF render failed: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        _discard(metadata)

    return RenderedPdf(
        path=target,
        page_count=_page_count(target),
        font_family=font_family,
        bytes_written=target.stat().st_size,
    )


class _LogCapture(logging.Handler):
    """Collect one logger's records for the duration of a `with` block.

    A context manager rather than a bare handler because the whole point is that the
    capture has a SCOPE: a handler left attached would keep accumulating every message for
    the rest of the process, which in a long agent run is a memory leak and — worse — a
    later render's failure could be attributed to an earlier one's output.

    The `pypandoc` logger specifically, and specifically because the signal this stage
    depends on arrives through `logging` and not through `sys.stderr`. Two
    implementations that looked for it in the wrong place are the reason this class has a
    docstring explaining what it is for.
    """

    LOGGER_NAME = "pypandoc"

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def __enter__(self) -> _LogCapture:
        self._logger = logging.getLogger(self.LOGGER_NAME)
        self._previous_level = self._logger.level
        # The logger's own level can be higher than WARNING, in which case attaching a
        # handler would capture nothing and the gate would be silently blind.
        if self._previous_level > logging.WARNING or self._previous_level == logging.NOTSET:
            self._logger.setLevel(logging.WARNING)
        self._logger.addHandler(self)
        return self

    def __exit__(self, *exc: object) -> None:
        self._logger.removeHandler(self)
        self._logger.setLevel(self._previous_level)


def _discard(path: Path) -> None:
    """Remove `path` if present, never raising. A cleanup that throws hides the real error."""
    try:
        path.unlink()
    except (FileNotFoundError, OSError):
        pass


def _page_count(path: Path) -> int:
    """Pages in `path`, counted from the raw bytes, or 0 when it cannot be read.

    Informational for the caller's log line, and deliberately NOT a gate. The object
    layout belongs to typst, and the count depends on it: typst writes `/Type/Page`
    WITHOUT a space (5 occurrences for a 4-page document, one of which is the single
    `/Type/Pages` tree node), so the subtraction is `/Type/Page` minus `/Type/Pages` and a
    naive substring count is off by exactly the number of page-tree nodes. That is a
    presentational detail of a third-party binary and this project does not branch on it —
    the document is correct whatever typst paginates it into, and 09-07's human pass is
    what judges the pagination. Page N of M in the footer is typst's own counter and is
    NOT derived from this number, which is why a wrong answer here cannot reach a reader.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return 0
    return max(0, raw.count(b"/Type/Page") - raw.count(b"/Type/Pages"))


# ============================================================ the CLI (wave 4)


def _parser() -> argparse.ArgumentParser:
    """Exactly three flags, like every other stage's CLI.

    `run_all` passes `--spec` and `--out` to all of them, and `--verbose` when the caller
    asked for it, so a fourth flag here would be a contract the orchestrator has to learn.
    The PDF stage is opt-in through the ORCHESTRATOR's `--pdf` (plan 09-06), not through a
    flag of its own: this stage's own settings are `--out` and nothing else, and a setting
    it does not own does not belong on its parser. CONTRACTS.md 8.8's own reasoning —
    "a fourth would be a contract the stages do not own" — applies unchanged.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, help="path to spec.json")
    parser.add_argument("--out", default="out", help="output directory (default: out)")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="logging-only debug diagnostics; adds no manifest field and changes no behavior",
    )
    return parser


def _append_format(manifest_path: Path, entry: Mapping[str, str]) -> None:
    """Add one `formats[]` object to the published manifest, atomically.

    The rule is CONTRACTS.md 8.1.1's and it is enforced by IMPORTING the same assertion the
    tests use, so the writer and the contract cannot disagree: at most one appended entry,
    the Markdown slot always first and byte-identical, no unratified `format` value, and
    `contract_version` still `report.v1`.

    Re-running is IDEMPOTENT and that is a requirement, not a nicety. `run_all` is
    re-runnable, and a second run with `--pdf` must not produce
    `[{markdown}, {pdf}, {pdf}]`. An entry already present, byte for byte, is left alone;
    the same `format` with a DIFFERENT `filename` is a conflict and refuses, because
    silently keeping either one would leave a manifest describing two documents.

    `report_is_stale` is not consulted and needs no change: it compares `metrics_sha256`
    and `charts_sha256`, and this writes neither.
    """
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    formats = manifest.get("formats")
    if not isinstance(formats, list):
        raise PdfError(
            f"build_pdf: {manifest_path} has no formats array; the report stage must run "
            f"first. Run the pipeline without --pdf, then again with it."
        )
    # The ALREADY-PUBLISHED check comes FIRST, and the first version of this function had
    # it second — which made a re-run propose `[{markdown}, {pdf}, {pdf}]` and then refuse
    # it as a clause-2 violation. The stage refused to do nothing, and the message blamed
    # the contract rather than the ordering. Idempotence is checked against what is on
    # disk; the contract is checked against what this run intends to publish.
    for existing in formats:
        if existing.get("format") == entry["format"]:
            if existing.get("filename") == entry["filename"]:
                return  # already published; re-running changes nothing
            raise PdfError(
                f"build_pdf: the manifest already lists format {entry['format']!r} as "
                f"{existing.get('filename')!r}, which is not {entry['filename']!r}. Refusing "
                f"to publish a manifest that names two files for one format."
            )
    proposed = [*formats, dict(entry)]
    # The contract is enforced on the value this run is about to publish, by the SAME
    # function the tests use, so the writer and the contract cannot drift.
    assert_formats_contract(proposed)
    manifest["formats"] = proposed
    # common.dump_json's discipline: same-directory staging plus one os.replace, so a
    # failed write leaves the previous manifest intact.
    dump_json(manifest, manifest_path)


def main(argv: Sequence[str] | None = None) -> int:
    """Publish `report.pdf` beside a report the report stage already wrote.

    Exit codes, the same table every stage uses so `run_all` needs no special case:

    * ``0`` — published, and `formats[]` now names it.
    * ``1`` — a local input, the toolchain, or an integrity failure. The message names the
      action; a model with no other context has to be able to act on it.
    * ``2`` — an invalid spec, and ONLY from `load_and_validate_spec`'s own
      `SystemExit(2)`, left uncaught so a spec problem is indistinguishable at the call site
      from a spec problem in any other stage. No return path here produces 2.

    The staleness refusal is the one worth reading twice. If the manifest's
    `metrics_sha256` does not match the `metrics.json` on disk, this stage exits 1 rather
    than rendering a PDF of numbers the manifest does not describe. Rendering anyway would
    produce a document that LOOKS current and is not — the failure mode every digest in
    `report.v1` exists to prevent, arriving one stage later.
    """
    args = _parser().parse_args(argv)
    setup_logging(args.verbose)
    spec = load_and_validate_spec(args.spec)   # SystemExit(2) inherited verbatim
    out_dir = Path(args.out)
    manifest_path = out_dir / build_report.REPORT_MANIFEST_FILENAME
    try:
        if not manifest_path.is_file():
            raise PdfError(
                f"build_pdf: {manifest_path} is missing. The PDF stage reads the report "
                f"stage's manifest; run the pipeline without --pdf first."
            )
        if not (out_dir / build_report.REPORT_FILENAME).is_file():
            raise PdfError(
                f"build_pdf: {out_dir / build_report.REPORT_FILENAME} is missing. A "
                f"manifest that names a report nobody can open describes nothing."
            )
        metrics, metrics_sha256 = make_charts.load_metrics(out_dir / "metrics.json")
        charts_document, charts_sha256 = build_report.load_charts(out_dir / "charts.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        recorded = manifest.get("metrics_sha256")
        if recorded != metrics_sha256:
            raise PdfError(
                f"build_pdf: the manifest records metrics_sha256 {recorded!r} but the "
                f"metrics.json on disk hashes to {metrics_sha256!r}. The report is stale; "
                f"re-run the pipeline without --pdf before rendering a PDF of it."
            )
        if manifest.get("charts_sha256") != charts_sha256:
            raise PdfError(
                f"build_pdf: the manifest's charts_sha256 does not match charts.json on "
                f"disk. Re-run the pipeline without --pdf first."
            )
        document = render_pdf_document(spec, metrics, charts_document)
        # The PDF lands first, and the manifest only after it is in place, so a manifest
        # never names a document that is not there (CONTRACTS.md 8.7's rule, inherited).
        rendered = render_pdf(
            document.body, out_dir=out_dir, spec_name=document.spec_name,
            as_of=document.as_of, language=document.language, images=document.images,
        )
        _append_format(manifest_path, {"format": "pdf", "filename": PDF_FILENAME})
    except (PdfError, ReportError, make_charts.ChartError,
            analyze_trends.AnalysisError) as error:
        print(f"pdf failed: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"pdf failed: could not read or write {out_dir}: {error}", file=sys.stderr)
        return 1
    print(
        f"Wrote PDF: {rendered.path}; pages: {rendered.page_count}; "
        f"font: {rendered.font_family}; manifest: {manifest_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())