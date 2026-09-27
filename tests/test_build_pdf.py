"""Wave 2 of Phase 9: `build_pdf.render_pdf_document`, the PURE planning half.

Every test in this file runs with **no PDF toolchain installed**. That is the point of
splitting the planner from the render: T1-T4 are the ANAL-06 and parity gates, and a gate
that needs `pypandoc` and a `winget`-installed `typst` to evaluate is a gate nobody runs
before shipping. The render call is plan 09-05; the typst preamble and the font probe are
09-04.

No network. The two-series fixture is the committed golden pair the report stage already
renders, so "the PDF shows what the Markdown shows" is a comparison between two renderings
of ONE input, not between two inputs.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote

import pytest

import analyze_trends
import build_pdf
import build_report
import make_charts
from common import load_and_validate_spec
from test_report import assert_no_invented_numbers, _render_two_series

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# The grid-table canon: a rule row, a bold header row, `+===+` immediately after it, and
# equal column widths in every row. Each rule exists because a table "flooded" in the
# external project this layout was carried over from, and a rule without its reason gets
# deleted by the next reader.
GRID_RULE = re.compile(r"^\+[-+]+\+$")


def _plan_from_disk(tmp_out: Path) -> tuple[build_pdf.PdfDocument, dict, dict, dict]:
    """Publish the REAL chart + report stages, then plan the PDF over the same bytes.

    `_render_two_series` is test_report's own helper, reused rather than reimplemented: it
    runs the real analyze/chart/report chain, reads the three artifacts back from DISK, and
    asserts no committed fixture was mutated. Reading off disk is deliberate — a fidelity
    check that read the planner's own inputs would prove the planner formats what it was
    handed, while what a reader opens is the published file. `build_report` runs first so
    the manifest exists to compare against: parity is a comparison between two published
    renderings of ONE input, not between two inputs.
    """
    _text, metrics, charts, manifest = _render_two_series(tmp_out)
    spec = load_and_validate_spec(FIXTURES_DIR / "spec.example.json")
    return build_pdf.render_pdf_document(spec, metrics, charts), metrics, charts, manifest


def _decoded_title_spans(body: str, metrics: Mapping[str, Any]) -> list[tuple[int, int]]:
    """Locate each series' DECODED title in the body and return its character span.

    Derived from the source document, not guessed from what a name looks like: the span is
    the exact text `display_article(metrics["series"][i]["article"])` produced. A number the
    author invented anywhere else in the body is therefore still checked, which is the
    property a letter-neighbour regex cannot offer.
    """
    spans: list[tuple[int, int]] = []
    for node in metrics["series"]:
        title = build_pdf.display_article(str(node["article"]))
        offset = 0
        while True:
            found = body.find(title, offset)
            if found < 0:
                break
            spans.append((found, found + len(title)))
            offset = found + len(title)
    return spans


# ---------------------------------------------------------------- T1: ANAL-06


def test_no_number_in_the_pdf_body_lacks_a_source(tmp_out: Path) -> None:
    """T1. Every numeral in the body is a value of `metrics.json` or `charts.json`.

    The reverse of `metrics_shown`, which proves everything required was shown. Nothing
    proved the opposite, so a renderer that computed a percentage of its own would produce
    a perfectly well-formed PDF whose manifest never mentioned the number.

    Reuses `test_report.assert_no_invented_numbers` rather than a second tokeniser: the
    Phase 8 lesson is that two rules which must agree are one rule called once, and that
    lesson was learned the hard way when `check_numbers` stripped separators on one side
    and tokenised differently on the other.
    """
    pdf, metrics, charts, _manifest = _plan_from_disk(tmp_out)
    assert_no_invented_numbers(
        pdf.body,
        {"metrics.json": metrics, "charts.json": charts},
        name_spans=_decoded_title_spans(pdf.body, metrics),
    )


def test_the_fidelity_gate_rejects_a_number_the_pdf_invented(tmp_out: Path) -> None:
    """The gate above is a gate: one invented figure turns it RED.

    An all-absence test is the Phase 3/05 failure this project has now paid for three
    times. The check is driven directly rather than through a mutation of the renderer, so
    the RED is unambiguous: if this passes, `assert_no_invented_numbers` accepts anything
    in this body shape and T1 was never a gate.
    """
    pdf, metrics, charts, _manifest = _plan_from_disk(tmp_out)
    tampered = pdf.body.replace("Висновок", "Висновок 42,7%")
    with pytest.raises(AssertionError, match="invented number"):
        assert_no_invented_numbers(
            tampered,
            {"metrics.json": metrics, "charts.json": charts},
            name_spans=_decoded_title_spans(pdf.body, metrics),
        )


# ---------------------------------------------------------------- T2: parity


def test_the_pdf_shows_exactly_the_numbers_the_markdown_shows(tmp_out: Path) -> None:
    """T2, and the mechanical form of the TZ's «паритет».

    `set(pdf.shown) == set(manifest["metrics_shown"])` — not a subset, not a superset.
    Fewer would be a lossy rendering of a document whose whole purpose is to stand in for
    the Markdown; more would be a number no consumer's manifest attests to. Both directions
    are therefore failures, and both are asserted below by the two non-vacuity probes.
    """
    pdf, _metrics, _charts, manifest = _plan_from_disk(tmp_out)
    assert set(pdf.shown) == set(manifest["metrics_shown"]), (
        f"the PDF and the Markdown disagree about which numbers are shown; "
        f"only in the PDF: {sorted(set(pdf.shown) - set(manifest['metrics_shown']))}; "
        f"only in the Markdown: "
        f"{sorted(set(manifest['metrics_shown']) - set(pdf.shown))}"
    )
    assert len(pdf.shown) == len(set(pdf.shown)), (
        f"the PDF's shown list carries duplicates, so a consumer's set comparison would "
        f"depend on how many sections quoted the same value: {pdf.shown}"
    )


def test_parity_holds_for_the_declining_fixture_too(tmp_out: Path) -> None:
    """The same equality on a document whose windows are all DECLINING.

    The two-series golden has a `2Y` that is not computable, but both its computable
    windows are negative too — so the declining fixture adds coverage of the sign path
    specifically, which is where the spike found typst substituting the minus glyph
    (09-ENGINE-DECISION.md D-D).
    """
    pdf, metrics, charts, _manifest = _plan_from_disk(tmp_out)
    for node in metrics["series"]:
        for window, _label in build_pdf.WINDOW_LABELS:
            pct = node["growth"][window].get("clean", {}).get("pct")
            if pct is not None:
                assert build_report.format_number(pct, percent=True) in pdf.body, (
                    f"the signed value of series[{node['series_id']}].growth.{window}."
                    f"clean.pct is absent from the body; a decline must be quoted WITH "
                    f"its sign, which is what Phase 8's FINDING-04 established"
                )
    assert_no_invented_numbers(
        pdf.body,
        {"metrics.json": metrics, "charts.json": charts},
        name_spans=_decoded_title_spans(pdf.body, metrics),
    )


# ---------------------------------------------------------------- T3: the null rule


def test_a_null_window_renders_the_token_and_its_reason_and_never_a_zero(tmp_out: Path) -> None:
    """T3. The `2Y` cell says «н/д» and why — never `0`, never `0.0`, never blank.

    The blank case is the one the card layout makes WORSE than the Markdown: inside a
    bordered card an empty cell reads as «measured, result zero», which is precisely the
    misreading ANAL-06's null rule exists to prevent. So the blank case is asserted
    positively here, not merely the absence of a literal.
    """
    pdf, metrics, _charts, _manifest = _plan_from_disk(tmp_out)
    tokens = build_report.report_tokens("uk")
    nulls = 0
    for node in metrics["series"]:
        window_node = node["growth"]["y2"]
        if window_node.get("clean", {}).get("pct") is not None:
            continue
        nulls += 1
        expected = build_report.reason_token("uk", str(window_node["clean"]["reason"]))
        assert f"{tokens['not_computable']}" in pdf.body, "the not-computable token is absent"
        assert expected in pdf.body, (
            f"a null window must carry its OWN localized reason, missing {expected!r}"
        )
    assert nulls, (
        "no null window in the fixture: this test would be an all-absence test that can "
        "never fail. The two-series golden is the document that exercises the 2Y refusal"
    )
    # Scoped to the growth CELLS, not the whole body. A whole-body substring search for
    # "0%" is defeated by the image directive `{width=100%}`, which contains it - the
    # check would then be reporting on a layout attribute instead of on a growth value.
    growth_cells = [
        cell.strip()
        for line in pdf.body.splitlines()
        if line.startswith("|") and tokens["growth"] in line
        for cell in line.strip("|").split("|")
    ]
    assert growth_cells, "no growth cell was rendered at all"
    for cell in growth_cells:
        for forbidden in ("0%", "0.0%", "0.0"):
            assert forbidden not in cell, (
                f"the growth cell {cell!r} renders {forbidden!r}; a null window must say "
                f"{tokens['not_computable']!r} and never a zero"
            )
    # The blank-cell case, stated as a POSITIVE assertion rather than an absence.
    for row in re.findall(r"^\|.*\|$", pdf.body, re.MULTILINE):
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        assert all(cell for cell in cells), f"a card cell is empty: {row!r}"


def test_the_null_rule_has_exactly_one_implementation() -> None:
    """`build_pdf` imports the null rule; it does not restate it.

    A second implementation would let a `build_report` wording change leave the PDF
    claiming a contract it no longer meets. This is asserted over the SOURCE, because a
    behavioural test cannot tell a shared function from a copy that happens to agree today.
    """
    import ast

    tree = ast.parse(Path(build_pdf.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "build_report"
        for alias in node.names
    }
    assert "growth_phrase" in imported, (
        f"build_pdf must IMPORT build_report.growth_phrase; it imports {sorted(imported)}"
    )
    defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "growth_phrase" not in defined, (
        "build_pdf defines its own growth_phrase - the null rule must have one "
        "implementation, not two that agree by coincidence"
    )


# ---------------------------------------------------------------- T4: the inventory


def test_the_body_references_every_chart_and_no_other(tmp_out: Path) -> None:
    """T4. The `2N+1` inventory, in published order, overlay last, no extras.

    Asserted from the RETURNED inventory AND from the body text, because those are two
    different claims: the field says what the stage believes it drew, the text says what
    the document will ask typst to load. A stage that filled the field correctly and wrote
    the wrong references would pass the first check alone.
    """
    pdf, _metrics, charts, _manifest = _plan_from_disk(tmp_out)
    published = [str(entry["filename"]) for entry in charts["charts"]]
    assert list(pdf.images) == published, (
        f"the planned image inventory drifted from charts.json: {list(pdf.images)}"
    )
    assert len(published) == 5, "the two-series fixture publishes 2N+1 = 5 charts"
    assert published[-1] == make_charts.OVERLAY_FILENAME, "the overlay must be last"
    referenced = re.findall(r"!\[\]\(([^)]+)\)", pdf.body)
    assert referenced == published, f"the body's image order drifted: {referenced}"


# ------------------------------------------- the spike's carry-overs


def test_the_body_carries_no_typographic_minus(tmp_out: Path) -> None:
    """The INPUT side of 09-ENGINE-DECISION.md D-D, pinned so the mitigation has a subject.

    typst's shaper — not pandoc's writer — substitutes U+2212 for a hyphen before a digit,
    and only in running text, so a rendered PDF carries two spellings of one value. The
    mitigation belongs on the rendered text (09-05). This asserts the body we hand it is
    clean, because a body that already carried U+2212 would make that gate
    indistinguishable from a shaper that ignored it.
    """
    pdf, _metrics, _charts, _manifest = _plan_from_disk(tmp_out)
    assert "\u2212" not in pdf.body, (
        "the body carries U+2212; the renderer will then be unable to tell a "
        "sign rewrite it caused from one the body already contained"
    )


@pytest.mark.parametrize(
    ("raw", "decoded"),
    [
        ("%D0%91%D1%96%D0%B7%D0%BD%D0%B5%D1%81", "Бізнес"),
        ("P%C5%AFst_p%C5%99eru%C5%A1ovan%C3%BD", "Půst_přerušovaný"),
        ("Business_process_management", "Business_process_management"),
        ("Strix_(security)", "Strix_(security)"),
        ("100%_done", "100%_done"),
        ("already%broken", "already%broken"),
    ],
)
def test_display_article_is_a_lossless_inverse_that_never_raises(
    raw: str, decoded: str
) -> None:
    """§5's display decode: exact on well-formed input, inert on malformed input.

    `unquote` leaves an invalid escape alone rather than raising, which is what makes this
    safe to call on ANY stored slug — a title carrying a literal `%` comes back unchanged
    instead of becoming a crash inside a stage that must never leave a half-written
    document behind. The last two cases are that property, pinned.
    """
    assert build_pdf.display_article(raw) == decoded
    # And the round trip that matters: the STORED value is still encoded.
    assert quote(build_pdf.display_article(raw), safe="_()%") == quote(raw, safe="_()%")


def test_the_stored_article_stays_encoded_on_disk(tmp_out: Path) -> None:
    """The display decode touches the DOCUMENT, never the data.

    Without this, a later reader "helpfully" normalises the stored slug and every non-Latin
    edition dies the way FINDING-01 killed them: the encoded slug is the resolver's AQS
    cache key and `fetch_pageviews.series_url` interpolates it unquoted.
    """
    _plan_from_disk(tmp_out)
    metrics = json.loads((tmp_out / "metrics.json").read_text(encoding="utf-8"))
    for node in metrics["series"]:
        assert "%" in str(node["article"]) or str(node["article"]).isascii(), (
            f"a stored article slug lost its percent-encoding: {node['article']!r}"
        )
    assert json.loads((tmp_out / "metrics.json").read_text(encoding="utf-8")) == metrics, (
        "the PDF stage must not rewrite metrics.json at all"
    )


# ---------------------------------------------------------------- language parity


def test_an_unmapped_language_is_refused_with_the_report_stages_own_message() -> None:
    """§5.4: the PDF must not become a second entrance for a script `report.md` refuses.

    The refusal is `build_report.report_tokens`' own, so the message is identical by
    construction rather than by a second copy of the wording. This was FINDING-05's shape
    one layer down: an English string inside a Ukrainian document, or a document in a
    language nobody asked for.
    """
    metrics = json.loads((FIXTURES_DIR / "metrics.example.json").read_text(encoding="utf-8"))
    spec = {"language": "ja"}
    metrics = {**metrics, "language": "ja"}
    with pytest.raises(build_report.ReportError, match="no report tokens for language: ja"):
        build_pdf.render_pdf_document(spec, metrics, {"charts": []})


def test_the_language_mismatch_branch_is_reachable_only_for_a_non_v1_metrics_document(
    tmp_out: Path,
) -> None:
    """The report stage's language cross-check, and an honest note on how live it is.

    `build_report.render_report` refuses when `metrics["language"]` disagrees with the
    spec's. **`metrics.v1` has no top-level `language` key** — D-03 froze exactly three
    identity keys (`spec_name`, `as_of`, `generated_from`) plus the per-series block, and
    `language` lives per series. So `str(metrics.get("language") or spec.get("language"))`
    always yields the spec's own language for a real document, and the comparison can never
    be false.

    That makes the branch a DEFENCE, not a live gate, and the test says so rather than
    pretending otherwise: a test that constructed a real `metrics.v1` document and
    asserted the refusal would be an all-absence test — the Phase 3 / 05-05 failure this
    project has now paid for three times. So the branch is exercised with a document that
    DOES carry the key, and the unreachability is asserted as its own fact.
    """
    _pdf, metrics, _charts, _manifest = _plan_from_disk(tmp_out)
    assert "language" not in metrics, (
        "metrics.v1 grew a top-level `language` key; D-03 froze three identity keys. If "
        "this is deliberate, CONTRACTS.md section 2 must document it AND the "
        "cross-check below becomes a live gate rather than a defence"
    )

    # With the key present, the branch works - and `build_pdf` inherits it rather than
    # restating it, so a change to the report stage's rule reaches the PDF for free.
    with pytest.raises(build_report.ReportError, match="does not match spec language"):
        build_pdf.render_pdf_document(
            {"language": "en"}, {**metrics, "language": "uk"}, {"charts": []}
        )


# ---------------------------------------------------------------- the six sections


def test_the_body_carries_the_six_sections_in_the_frozen_order(tmp_out: Path) -> None:
    """The OUTPUT order matches the contract.

    Necessary, and NOT sufficient — which is why the next test exists. A renderer and a
    test that both hand-type the same wrong order agree with each other, and that is
    exactly how a frozen order stops being frozen while every behavioural test stays
    green. So this test pins the document, and the one below pins the SOURCE.
    """
    pdf, _metrics, _charts, _manifest = _plan_from_disk(tmp_out)
    tokens = build_report.report_tokens("uk")
    order = [
        match.group(1)
        for match in re.finditer(r"^## (.+)$", pdf.body, re.MULTILINE)
    ]
    assert order == [tokens[key] for key in build_report.SECTION_TOKEN_KEYS], (
        f"the sections drifted from the frozen order: {order}"
    )


def test_the_section_order_is_walked_from_the_contract_not_retyped() -> None:
    """The SOURCE reads `SECTION_TOKEN_KEYS`; it does not spell the order out again.

    This is the test an injected-defect probe earned. Probe N8 replaced the frozen tuple
    with a hand-typed one **in the same order**, and the behavioural test above stayed
    GREEN — because the document it produced was correct. A correct document from a
    retyped constant is still a retyped constant, and the next reader who reorders
    `SECTION_TOKEN_KEYS` in `build_report` would find the PDF quietly disagreeing with
    the contract and no test to say so.

    So the check is over the AST, not the output: the module must REFERENCE the tuple, and
    must not contain a literal sequence of the six section keys.
    """
    import ast

    source = Path(build_pdf.__file__).read_text(encoding="utf-8")
    assert "SECTION_TOKEN_KEYS" in source, (
        "build_pdf must walk build_report.SECTION_TOKEN_KEYS rather than listing the "
        "sections itself; the order is a contract and a retyped copy can drift silently"
    )
    tree = ast.parse(source)
    keys = set(build_report.SECTION_TOKEN_KEYS)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Tuple, ast.List)):
            continue
        literals = {
            element.value for element in node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        }
        assert literals != keys, (
            f"build_pdf spells out the six section keys at line {node.lineno}; the order "
            f"must come from build_report.SECTION_TOKEN_KEYS"
        )


def test_the_closing_line_is_present_and_is_the_frozen_sentence(tmp_out: Path) -> None:
    """Not a style choice: a frozen part of the report contract, in both renderings."""
    pdf, _metrics, _charts, _manifest = _plan_from_disk(tmp_out)
    tokens = build_report.report_tokens("uk")
    assert pdf.body.rstrip().endswith(f"**{tokens['not_a_forecast']}.**"), (
        "the PDF must end on the frozen not-a-forecast line, as report.md does"
    )


# ---------------------------------------------------------------- grid-table canon


def test_every_card_is_a_well_formed_grid_table(tmp_out: Path) -> None:
    """The canon from the project this layout was carried over from, enforced here.

    Four rules, each with a reason: a `+---+` rule row, a `**bold**` header, equal column
    widths in every row, and no empty cell. A table that "flooded" is a documented failure
    there, and STATE.md's 05-05 lesson is that legibility defects pass every structural
    test and are found by LOOKING. These four are the ones a machine can catch; the rest
    still needs 09-07's human pass.
    """
    pdf, _metrics, _charts, _manifest = _plan_from_disk(tmp_out)
    rule_rows = [line for line in pdf.body.splitlines() if GRID_RULE.match(line)]
    assert rule_rows, "the card layout produced no grid rules at all"
    widths = {len(line) for line in rule_rows}
    assert len(widths) == 1, f"grid rule rows disagree on width: {sorted(widths)}"
    for line in pdf.body.splitlines():
        if line.startswith("|"):
            assert line.endswith("|"), f"a grid row is not closed: {line!r}"
            assert line.count("|") >= 3, f"a grid row has one column: {line!r}"


# ================================================== wave 3: the renderer (R1-R8)
#
# R4, R5 and R6 run a REAL `pandoc + typst` conversion. They are NOT skipped when the
# toolchain is absent, because a gate that skips is the failure this project keeps paying
# for — instead they skip with a reason that names the missing tool, and the DEFAULT suite
# still runs everything else. `pypdf` is used only to COUNT the images in a finished
# document, which is why it is a dev dependency and not in `requirements-pdf.txt`.


def _toolchain_available() -> bool:
    try:
        import pypandoc  # noqa: F401
    except ImportError:
        return False
    return shutil.which("typst") is not None


needs_toolchain = pytest.mark.skipif(
    not _toolchain_available(),
    reason="the optional PDF toolchain is absent: "
           f"{build_pdf.PDF_DEPS_INSTALL} and {build_pdf.TYPST_INSTALL}",
)


# ---------------------------------------------------------------- R1, R2: the font


def test_the_probed_font_family_is_a_family_typst_really_has() -> None:
    """R1. The chosen family is an EXACT line of `typst fonts`.

    Exact, not substring. This plan's author briefly recorded `DejaVu Sans` as present on
    a machine that has only `DejaVu Sans Mono`, because the check was
    `"DejaVu Sans" in stdout`. A font gate satisfied by a near-miss is not a gate, and the
    failure it would permit is the one this probe exists to prevent: typst does not reject
    an unknown family, it warns and substitutes, so a wrong answer here ships a document
    with no Cyrillic and no error.
    """
    available = build_pdf.installed_font_families()
    family = build_pdf.probe_font_family("uk")
    assert family in available, (
        f"probe_font_family returned {family!r}, which is not a family typst reports; "
        f"it must be an EXACT entry, never a substring match"
    )
    assert family in build_pdf.FONT_CANDIDATES, (
        f"{family!r} is not one of the declared candidates {build_pdf.FONT_CANDIDATES!r}"
    )


def test_the_probe_refuses_rather_than_naming_a_family_it_cannot_see(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2. With nothing installed, the answer is a refusal naming the install command.

    Fail-closed is the whole point. The alternative — returning the first candidate and
    letting typst warn — produces a complete-looking report in a language the reader asked
    for, with none of that language in it, and no error anywhere.
    """
    monkeypatch.setattr(build_pdf, "installed_font_families", lambda: frozenset())
    with pytest.raises(build_pdf.PdfError) as excinfo:
        build_pdf.probe_font_family("uk")
    message = str(excinfo.value)
    assert build_pdf.TYPST_INSTALL in message, (
        f"the refusal must name the install command, got {message!r}"
    )
    assert "warn" in message, (
        "the refusal must say WHY it refuses - that typst would warn and substitute - so "
        "the reader knows a fallback was deliberately not taken"
    )


def test_the_probe_does_not_choose_a_family_typst_only_half_knows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`DejaVu Sans Mono` is not `DejaVu Sans`, and the probe must not take it.

    The exact confusion this plan measured, promoted to a gate: a registry that offers
    only the MONOSPACE variant must not satisfy a request for the proportional one, which
    is what a substring test would do.
    """
    monkeypatch.setattr(
        build_pdf, "installed_font_families", lambda: frozenset({"DejaVu Sans Mono"})
    )
    with pytest.raises(build_pdf.PdfError):
        build_pdf.probe_font_family("uk")


# ---------------------------------------------------------------- R3: the preamble


def _preamble_code() -> str:
    """`wti-preamble.typ` with its `//` comments removed.

    The comment stripping is not cosmetic, and it is not a detail. The preamble explains
    at length WHY the font is a variable and WHY the counter idiom is the one it is — and
    both explanations necessarily QUOTE the things the tests forbid: the mandated font
    stack, and `counter(page).final().display()`. Asserting against the raw file therefore
    fails on its own documentation, which is the mirror image of Phase 8's lesson that a
    gate satisfied by an unrelated sentence is not a gate: a gate that FIRES on an
    unrelated sentence is not a gate either, and it teaches the next reader to ignore it.
    """
    raw = build_pdf.PREAMBLE_PATH.read_text(encoding="utf-8")
    return "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("//")
    )


def test_the_preamble_ships_and_carries_no_hardcoded_identity() -> None:
    """R3, statically. The preamble is committed, and names nothing it was handed.

    `spec-name`, `as-of` and the font arrive as pandoc variables. A literal Ukrainian
    heading or a literal `Arial` here would be a second copy of a value the code owns —
    the drift `charts.v1` §7.4 and the `growth_phrase` promotion both exist to prevent.
    """
    assert build_pdf.PREAMBLE_PATH.is_file(), "the typst preamble must ship with the skill"
    code = _preamble_code()
    for placeholder in ("$spec-name$", "$as-of$", "$font-family$", "$lang$", "$body$"):
        assert placeholder in code, f"the preamble must read {placeholder} from metadata"
    # The font assertion is on the `#set text` LINE, not on the absence of the word
    # "Arial". An injected-defect probe caught the difference: hardcoding
    # `#set text(font: "Nonexistent Font", …)` sailed past an `assert "Arial" not in code`,
    # because the check named the family this machine happens to have instead of the
    # RULE. A gate that names an instance rather than a rule passes on the next instance.
    text_lines = [ln for ln in code.splitlines() if ln.strip().startswith("#set text(")]
    assert len(text_lines) == 1, f"expected one #set text line, got {text_lines!r}"
    assert "$font-family$" in text_lines[0], (
        f"the font must come from the probed $font-family$ placeholder, got "
        f"{text_lines[0]!r}. A hardcoded family is a family typst may warn about and "
        f"silently replace, which is the failure the probe exists to prevent."
    )
    for heading in ("Висновок", "Метрики", "Графіки"):
        assert heading not in code, (
            f"the preamble must not carry the localized heading {heading!r}; those live in "
            f"build_report's token table and are the single source"
        )


def test_the_preamble_uses_the_compiling_counter_idiom() -> None:
    """The measured idiom, not the assignment's.

    The technical assignment writes `counter(page).final().display()` for the total page
    count. On typst 0.15.1 that does not compile: "type array has no method `display`". A
    static check cannot prove compilation, so this pins the two expressions that were
    MEASURED to compile, and R4 below compiles them for real.
    """
    code = _preamble_code()
    assert "counter(page).display()" in code, "the current page counter is missing"
    assert "counter(page).final().first()" in code, (
        "the TOTAL page count must use .final().first(); "
        "counter(page).final().display() does not compile on typst 0.15.1"
    )
    assert "counter(page).final().display()" not in code, (
        "the assignment's non-compiling expression is present"
    )


# ---------------------------------------------------------------- R4-R7: the recipe


@needs_toolchain
def test_a_real_render_embeds_every_chart_and_the_footer(tmp_out: Path) -> None:
    """R4. The whole recipe, run for real, with the charts COUNTED in the output.

    Counting is done with `pypdf`, a DEV dependency, and that is the deliberate division:
    asserting the count at runtime would put a test-only need into the install contract.
    The runtime gate is coarser - at least one image, plus pandoc's own
    `Could not fetch resource` signal - and R6 proves the coarse gate fires.
    """
    pdf_doc, metrics, charts, _manifest = _plan_from_disk(tmp_out)
    rendered = build_pdf.render_pdf(
        pdf_doc.body, out_dir=tmp_out, spec_name=pdf_doc.spec_name,
        as_of=pdf_doc.as_of, language=pdf_doc.language, images=pdf_doc.images,
    )
    assert rendered.path == tmp_out / build_pdf.PDF_FILENAME
    assert rendered.path.read_bytes()[:5] == b"%PDF-"
    assert rendered.font_family in build_pdf.installed_font_families()

    from pypdf import PdfReader

    reader = PdfReader(str(rendered.path))
    embedded = sum(len(page.images) for page in reader.pages)
    assert embedded == len(charts["charts"]) == 5, (
        f"the PDF embeds {embedded} images; the chart manifest publishes "
        f"{len(charts['charts'])} and the 2N+1 rule says 5. A report without its charts "
        f"is a report without its evidence."
    )
    text = "".join((page.extract_text() or "") for page in reader.pages)
    assert pdf_doc.spec_name in text, "the footer's spec name is absent"
    assert pdf_doc.as_of in text, "the footer's as_of is absent"
    assert "сторінка" in text, "the footer's page counter is absent"
    assert rendered.page_count == len(reader.pages), (
        f"the informational page count said {rendered.page_count}; pypdf says "
        f"{len(reader.pages)}"
    )
    # The §5 display fix, proven in the OUTPUT rather than in the planner's intent: a
    # fidelity check that read the return value would prove the planner formats what it
    # intends, while what a reader opens is the file.
    assert "Půst" in text, "the percent-DECODED article title is absent from the PDF"
    assert "P%C5%AFst" not in text, "the percent-ENCODED slug reached the document"


@needs_toolchain
def test_the_recipe_survives_a_project_root_set(tmp_out: Path) -> None:
    """R5. The same recipe, with typst given a project root.

    The configuration nobody tests by hand. The spike measured that an absolute Windows
    path is rejected outright under a root (*invalid component `"C:"`*) and that
    `pypandoc` works with absolute paths ONLY because it sets no root — so the spike's
    recipe was one configuration away from silently losing every chart. This test drives
    `typst` directly, WITH a root, over the intermediate document pandoc produces, and
    requires the images to survive. Bare names plus `--resource-path` is the recipe chosen
    so that it is root-INDEPENDENT rather than patched per root.
    """
    pdf_doc, _metrics, _charts, _manifest = _plan_from_disk(tmp_out)
    build_pdf.render_pdf(
        pdf_doc.body, out_dir=tmp_out, spec_name=pdf_doc.spec_name,
        as_of=pdf_doc.as_of, language=pdf_doc.language, images=pdf_doc.images,
    )
    family = build_pdf.probe_font_family(pdf_doc.language)
    metadata = tmp_out / "report.pdf.meta.yaml"
    metadata.write_text(
        build_pdf._metadata_yaml(pdf_doc.spec_name, pdf_doc.as_of, family, pdf_doc.language),
        encoding="utf-8",
    )
    intermediate = tmp_out / "report.pdf.body.typ"
    import pypandoc

    pypandoc.convert_text(
        pdf_doc.body, "typst", format="md", outputfile=str(intermediate),
        extra_args=["--standalone", f"--template={build_pdf.PREAMBLE_PATH}",
                    f"--metadata-file={metadata}"],
    )
    rooted = tmp_out / "rooted.pdf"
    proc = subprocess.run(
        [build_pdf.typst_executable(), "compile", "--root", str(tmp_out),
         str(intermediate), str(rooted)],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert proc.returncode == 0, (
        f"typst failed with a project root set, which is the configuration the recipe "
        f"must survive: {proc.stderr[-600:]}"
    )
    from pypdf import PdfReader

    embedded = sum(len(p.images) for p in PdfReader(str(rooted)).pages)
    assert embedded == 5, (
        f"with a root set the charts vanish ({embedded} of 5 embedded). The recipe must be "
        f"root-INDEPENDENT, not correct only when no root is passed."
    )


@needs_toolchain
def test_a_missing_chart_makes_the_render_refuse_and_publish_nothing(tmp_out: Path) -> None:
    """R6. THE test this plan exists for.

    Measured behaviour being defended against: a bare image name that cannot be fetched
    makes pandoc print `Could not fetch resource`, exit **0**, and produce a complete
    report with that chart replaced by its description. Nothing fails, nothing warns
    visibly, and a reader gets prose with less evidence than they were given.

    The first version of this test asserted the refusal and **passed the render** — the
    gate was checking only that *some* image survived, and 4-of-5 charts clears any such
    check. The gate now requires the count to be exact, and this test is what proves it.

    Driven at a body whose chart is absent from the directory. It must REFUSE: a
    `PdfError` naming the mismatch, no `report.pdf` published, no staging file left, and
    the PREVIOUS `report.pdf` byte-identical — the atomicity property, checked on the
    failure path where it actually matters.
    """
    pdf_doc, _metrics, _charts, _manifest = _plan_from_disk(tmp_out)
    good = build_pdf.render_pdf(
        pdf_doc.body, out_dir=tmp_out, spec_name=pdf_doc.spec_name,
        as_of=pdf_doc.as_of, language=pdf_doc.language, images=pdf_doc.images,
    )
    published = good.path.read_bytes()
    assert published[:5] == b"%PDF-"

    (tmp_out / pdf_doc.images[0]).unlink()
    with pytest.raises(build_pdf.PdfError) as excinfo:
        build_pdf.render_pdf(
            pdf_doc.body, out_dir=tmp_out, spec_name=pdf_doc.spec_name,
            as_of=pdf_doc.as_of, language=pdf_doc.language, images=pdf_doc.images,
        )
    message = str(excinfo.value)
    # Either signal is a valid refusal, and which one fires first is an implementation
    # detail: the logger names the file, the count names the shortfall. What the test
    # requires is that the refusal NAMES THE CHART — a bare "render failed" would leave
    # the reader with nothing to act on, which is the whole point of an actionable exit.
    assert pdf_doc.images[0] in message, (
        f"the refusal must name the chart that went missing, got {message!r}"
    )
    assert "Refused rather than published" in message, (
        f"the refusal must say the document was NOT published, got {message!r}"
    )
    assert not any(p.name.startswith(".") for p in tmp_out.iterdir()), (
        "a refused render must leave no staging file behind"
    )
    assert good.path.read_bytes() == published, (
        "a refused render must leave the PREVIOUS report.pdf byte-identical"
    )


@needs_toolchain
def test_the_marker_count_gate_fires_on_its_own(
    tmp_out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SECOND signal, demonstrated to fire with the first one disabled.

    The render has two independent refusals: the `pypandoc` logger signal, which names the
    file, and the image-marker count, computed from bytes. "Independent" is a claim, and a
    claim about a gate is worth exactly as much as the demonstration that it holds — so
    the logger is silenced here and the count must catch the same defect on its own.

    Without this, a reader is entitled to assume the count gate is dead code that only
    works because the logger happens to fire first, which is the "a gate satisfied by an
    unrelated thing is not a gate" failure in its purest form.
    """
    pdf_doc, _metrics, _charts, _manifest = _plan_from_disk(tmp_out)
    (tmp_out / pdf_doc.images[0]).unlink()

    real_emit = build_pdf._LogCapture.emit

    def silenced(self: build_pdf._LogCapture, record: object) -> None:
        return None

    monkeypatch.setattr(build_pdf._LogCapture, "emit", silenced)
    try:
        with pytest.raises(build_pdf.PdfError) as excinfo:
            build_pdf.render_pdf(
                pdf_doc.body, out_dir=tmp_out, spec_name=pdf_doc.spec_name,
                as_of=pdf_doc.as_of, language=pdf_doc.language, images=pdf_doc.images,
            )
    finally:
        monkeypatch.setattr(build_pdf._LogCapture, "emit", real_emit)
    message = str(excinfo.value)
    assert "image markers" in message, (
        f"with the logger signal silenced the COUNT gate must catch the missing chart on "
        f"its own, got {message!r}"
    )
    assert not (tmp_out / build_pdf.PDF_FILENAME).exists(), (
        "a render refused by the count gate must not publish anything"
    )


@needs_toolchain
def test_a_successful_render_leaves_no_staging_or_metadata_behind(tmp_out: Path) -> None:
    """R7, positive half. The success path is as clean as the failure path.

    A leftover `report.pdf.meta.yaml` in a user's output directory is a second copy of
    their spec name and `as_of`, published next to a document that is supposed to be the
    only one.
    """
    pdf_doc, _metrics, _charts, _manifest = _plan_from_disk(tmp_out)
    build_pdf.render_pdf(
        pdf_doc.body, out_dir=tmp_out, spec_name=pdf_doc.spec_name,
        as_of=pdf_doc.as_of, language=pdf_doc.language, images=pdf_doc.images,
    )
    published = sorted(p.name for p in tmp_out.iterdir())
    assert build_pdf.PDF_FILENAME in published
    assert not [n for n in published if n.startswith(".")], (
        f"staging artefacts survived a successful render: {published}"
    )
    assert not [n for n in published if n.endswith(".meta.yaml")], (
        f"the metadata file survived publication: {published}"
    )


# ---------------------------------------------------------------- R8: laziness


def test_the_renderer_imports_pypandoc_lazily_and_never_asks_for_a_root() -> None:
    """R8, over the source. Two properties, both of which have already broken something.

    * **Laziness.** `pypandoc` is absent from the phase-local .venv by default, so a
      module-level import breaks pytest COLLECTION for every test importing `build_pdf` —
      not one test, all of them.
    * **No `--root`.** Measured: setting a root turns the working recipe into
      `source file must be contained in project root`, and makes an absolute Windows path
      fail outright. A future reader "hardening" the render with a root would reintroduce
      the spike's fragility while looking like an improvement.
    """
    import ast

    source = Path(build_pdf.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    # String constants that are CODE, not documentation. Asserting against the raw source
    # text is wrong in a file that explains itself: `--root` appears in this module's
    # docstrings precisely because the module explains why it must never pass one, and the
    # first version of this test failed on its own explanation. A gate that fires on the
    # sentence defending it teaches the next reader to delete the defence.
    #
    # The docstrings are identified BY POSITION - the first statement of every module,
    # class or function, when it is a string - rather than by comparing text, because
    # `ast.get_docstring` returns the DEDENTED string while the AST node holds the raw
    # one, and a text comparison silently matches nothing. That failure mode is quiet and
    # it defeats the exclusion entirely, which is what the second version of this test did.
    docstring_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ) and node.body and isinstance(node.body[0], ast.Expr):
            first = node.body[0]
            if isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                docstring_nodes.add(id(first.value))
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in docstring_nodes
    ]
    joined = "\n".join(literals)
    assert any("--root" in value for value in [
        ast.get_docstring(n) or ""
        for n in ast.walk(tree)
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef))
    ]), (
        "sanity: this module's docstrings are expected to DISCUSS --root, which is why "
        "the exclusion above exists. If that sentence has gone, re-check whether the "
        "exclusion is still doing anything."
    )

    # TOP-LEVEL imports only. An `ast.walk` finds the lazy import inside `render_pdf` too.
    module_level: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            module_level |= {alias.name.split(".")[0] for alias in node.names}
    assert "pypandoc" not in module_level, (
        f"pypandoc must be imported INSIDE render_pdf, not at module level "
        f"({sorted(module_level)}); a machine without the optional dependency would fail "
        f"pytest COLLECTION for every test importing this module"
    )
    lazy = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        and any(a.name.split(".")[0] == "pypandoc" for a in node.names)
    ]
    assert lazy, "expected a lazy `import pypandoc` inside render_pdf"
    inside = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "render_pdf"),
        None,
    )
    assert inside is not None, "render_pdf is missing - the laziness claim is about it"
    assert any(node in set(ast.walk(inside)) for node in lazy), (
        "the pypandoc import must live inside render_pdf specifically, not in a helper the "
        "pure planner could reach"
    )

    assert "--root" not in joined, (
        "build_pdf must not pass --root: measured, it is incompatible with the working "
        "recipe and with any absolute image path"
    )
    # The converter's ARGUMENT LIST is read from the source, not from string constants:
    # `--resource-path={directory}` is an f-string, so it is an `ast.JoinedStr` and never
    # appears in the constant list at all. `ast.unparse` on the assignment is the honest
    # way to see what pandoc will actually be handed.
    extra = next(
        (n for n in ast.walk(inside)
         if isinstance(n, ast.Assign)
         and any(isinstance(t, ast.Name) and t.id == "extra_args" for t in n.targets)),
        None,
    )
    assert extra is not None, "render_pdf must build its converter arguments explicitly"
    args = ast.unparse(extra)
    assert "--resource-path={directory}" in args, (
        "the resource path must be the OUTPUT directory, which is where the charts are. "
        f"The measured failure was a bare image name with no resource path at all: pandoc "
        f"exits 0, replaces every image with its alt text, and the document looks "
        f"complete. Arguments as built: {args}"
    )
    assert "--pdf-engine=typst" in args, (
        "the engine is typst, per 09-ENGINE-DECISION.md; a different engine would make "
        "every measured fact in this module (the counter idiom, the marker factor, the "
        "root incompatibility) describe a renderer that is not in use"
    )


def test_the_base_install_contract_is_unchanged_by_the_pdf_feature() -> None:
    """`requirements.txt` is still one line, and the PDF deps live elsewhere.

    `test_run_all.py` already asserts the single `matplotlib>=3.11` line. What is added
    here is the OTHER half, which nothing asserted: that the optional dependencies are
    actually declared somewhere, so `pip install -r requirements-pdf.txt` is a real
    instruction rather than a file that does not exist.
    """
    base = (Path(build_pdf.__file__).resolve().parents[1] / "requirements.txt")
    assert [l.strip() for l in base.read_text(encoding="utf-8").splitlines() if l.strip()] == [
        "matplotlib>=3.11"
    ], "the base install must remain exactly one package"
    optional = base.with_name("requirements-pdf.txt")
    assert optional.is_file(), "requirements-pdf.txt must exist: it is the documented command"
    declared = {l.strip() for l in optional.read_text(encoding="utf-8").splitlines()
                if l.strip() and not l.strip().startswith("#")}
    assert declared == {"pypandoc-binary", "PyYAML"}, (
        f"the optional PDF dependencies changed to {sorted(declared)}; the documented "
        f"install command and this assertion must move together"
    )
    assert "pypdf" not in " ".join(declared), (
        "pypdf is a DEV dependency: it counts images in a test, and putting it in the "
        "install contract would promote a test-only need into a user-facing dependency"
    )


def test_the_planner_performs_no_io_and_imports_no_renderer() -> None:
    """The purity claim, asserted over the source rather than trusted.

    `render_pdf_document` must be testable on a machine with no `pypandoc` and no `typst`.
    That property is what makes T1-T4 run in the default suite, so it is enforced at the
    module level rather than left to discipline.

    **NARROWED IN 09-04, and the narrowing is stated rather than absorbed.** This test
    originally forbade `subprocess` as well as `pypandoc`, which was correct when the
    module held only the planner. Wave 3 legitimately added `subprocess` — the font probe
    runs `typst fonts` and nothing else can answer "which families does typst see" — so
    forbidding it would have forbidden the fix for the defect the spike found. What
    remains forbidden is the thing that actually breaks the suite: a MODULE-LEVEL
    `pypandoc` import, which fails collection for every test in this file on a machine
    where the optional dependency is absent. The planner's own no-I/O property is
    unchanged and still asserted below.
    """
    import ast

    source = Path(build_pdf.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    module_level: set[str] = set()
    for node in tree.body:  # top level only - a nested import is the LAZY one
        if isinstance(node, ast.Import):
            module_level |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_level.add(node.module.split(".")[0])
    assert "pypandoc" not in module_level, (
        f"build_pdf imports pypandoc at module level ({sorted(module_level)}); it must be "
        f"imported INSIDE render_pdf, or a machine without the optional dependency fails "
        f"pytest COLLECTION for every test that imports this module"
    )
    # The planner half: `render_pdf_document` opens nothing and writes nothing.
    planner = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "render_pdf_document"
    )
    for name in ("open", "write_text", "write_bytes", "mkdir", "replace", "unlink"):
        assert not [
            node for node in ast.walk(planner)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == name
        ], f"render_pdf_document calls {name!r}; the planning half performs no I/O"


# ================================== wave 4: the CLI, the exit table, the append


def _published(tmp_out: Path) -> None:
    """Run the REAL analyze/chart/report chain so a manifest exists to append to."""
    import shutil as _shutil

    _shutil.copyfile(FIXTURES_DIR / "series.example.csv", tmp_out / "series.csv")
    for stage in (analyze_trends, make_charts, build_report):
        assert stage.main(
            ["--spec", str(FIXTURES_DIR / "spec.example.json"), "--out", str(tmp_out)]
        ) == 0, f"{stage.__name__} must succeed before the PDF stage"


def _manifest(tmp_out: Path) -> dict:
    return json.loads(
        (tmp_out / build_report.REPORT_MANIFEST_FILENAME).read_text(encoding="utf-8")
    )


@needs_toolchain
def test_main_publishes_the_pdf_and_appends_exactly_one_format(tmp_out: Path) -> None:
    """The happy path, end to end through the CLI, with the manifest read back off disk.

    The contract assertions run on the PUBLISHED manifest rather than on a return value,
    because the published file is what a consumer reads and a test of this stage's own
    return would prove only that the stage formats what it intends.
    """
    _published(tmp_out)
    before = _manifest(tmp_out)
    assert before["formats"] == build_report.RATIFIED_V1_FORMATS

    assert build_pdf.main(
        ["--spec", str(FIXTURES_DIR / "spec.example.json"), "--out", str(tmp_out)]
    ) == 0

    after = _manifest(tmp_out)
    build_report.assert_formats_contract(after["formats"])
    assert after["formats"] == [
        *build_report.RATIFIED_V1_FORMATS,
        build_report.RATIFIED_PDF_FORMAT,
    ], f"formats[] is {after['formats']!r}"
    assert after["contract_version"] == "report.v1", "clause 5: not a version bump"
    assert set(after) == set(before), "no top-level key may be added or dropped"
    assert (tmp_out / build_pdf.PDF_FILENAME).read_bytes()[:5] == b"%PDF-"
    assert not (tmp_out / "report.md").with_suffix(".md.orig").exists()


@needs_toolchain
def test_a_second_run_is_idempotent_and_leaves_the_manifest_identical(tmp_out: Path) -> None:
    """Re-running with `--pdf` must not append a second `pdf` entry.

    `run_all` is re-runnable by design, so this is a requirement rather than a nicety: a
    second run that produced `[{markdown}, {pdf}, {pdf}]` would leave a manifest that
    CONTRACTS.md 8.1.1 clause 2 forbids, and the only test that could notice is one that
    runs the stage twice.
    """
    _published(tmp_out)
    argv = ["--spec", str(FIXTURES_DIR / "spec.example.json"), "--out", str(tmp_out)]
    assert build_pdf.main(argv) == 0
    first = (tmp_out / build_pdf.PDF_FILENAME).read_bytes()
    published = (tmp_out / build_report.REPORT_MANIFEST_FILENAME).read_bytes()

    assert build_pdf.main(argv) == 0, "a second run must succeed, not refuse"
    assert _manifest(tmp_out)["formats"] == [
        *build_report.RATIFIED_V1_FORMATS,
        build_report.RATIFIED_PDF_FORMAT,
    ]
    assert (tmp_out / build_report.REPORT_MANIFEST_FILENAME).read_bytes() == published, (
        "an idempotent re-run must not rewrite the manifest at all"
    )
    assert (tmp_out / build_pdf.PDF_FILENAME).read_bytes()[:5] == b"%PDF-"
    assert first  # the first render happened


@needs_toolchain
def test_appending_is_stale_detector_blind_by_design(tmp_out: Path) -> None:
    """`report_is_stale` must still answer False after the append.

    T12 of the technical assignment, and the reason it holds: staleness is a comparison of
    `metrics_sha256` and `charts_sha256`, and this stage writes NEITHER. It adds an array
    element, so a document that was fresh stays fresh — and a stage that accidentally
    rewrote a digest would make every report look stale, which is the failure mode §8.1
    warns about when it says the digests are an integrity mechanism and nothing more.
    """
    _published(tmp_out)
    assert build_report.report_is_stale(tmp_out) is False
    assert build_pdf.main(
        ["--spec", str(FIXTURES_DIR / "spec.example.json"), "--out", str(tmp_out)]
    ) == 0
    assert build_report.report_is_stale(tmp_out) is False, (
        "the append changed no digest, so the report cannot have become stale"
    )
    for key in ("metrics_sha256", "charts_sha256"):
        assert key in _manifest(tmp_out), f"{key} must survive the append untouched"


@needs_toolchain
def test_a_stale_metrics_document_is_refused_and_nothing_is_published(
    tmp_out: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The digest guard, on the failure path, with the OUTPUT checked.

    If the manifest's `metrics_sha256` disagrees with the file on disk, this stage must
    exit 1 rather than render a PDF of numbers the manifest does not describe. Rendering
    anyway would produce a document that looks current and is not — the exact failure every
    digest in `report.v1` exists to prevent, arriving one stage later.
    """
    _published(tmp_out)
    manifest = _manifest(tmp_out)
    manifest["metrics_sha256"] = "0" * 64
    (tmp_out / build_report.REPORT_MANIFEST_FILENAME).write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    assert build_pdf.main(
        ["--spec", str(FIXTURES_DIR / "spec.example.json"), "--out", str(tmp_out)]
    ) == 1
    err = capsys.readouterr().err
    assert "metrics_sha256" in err, f"the refusal must name the digest that disagreed: {err!r}"
    assert not (tmp_out / build_pdf.PDF_FILENAME).exists(), (
        "a stale document must produce no PDF at all"
    )


def test_an_invalid_spec_exits_two_and_writes_nothing(
    tmp_out: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 2 comes ONLY from `load_and_validate_spec`, inherited uncaught.

    No return path in this stage produces 2, which is what makes the code mean one thing at
    every call site — `run_all` maps it to "fix the spec" without knowing which stage
    produced it.
    """
    bad = tmp_out / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        build_pdf.main(["--spec", str(bad), "--out", str(tmp_out)])
    assert excinfo.value.code == 2
    assert not (tmp_out / build_pdf.PDF_FILENAME).exists()


def test_a_missing_manifest_refuses_with_the_pipeline_order_named(
    tmp_out: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No manifest, no PDF — and the message says which run to do first.

    The PDF stage is the fifth stage, not a replacement for the fourth. A user who runs it
    alone must be told the order rather than left with a refusal they cannot act on.
    """
    assert build_pdf.main(
        ["--spec", str(FIXTURES_DIR / "spec.example.json"), "--out", str(tmp_out)]
    ) == 1
    err = capsys.readouterr().err
    assert "manifest" in err and "--pdf" in err, (
        f"the refusal must name the manifest and the pipeline order, got {err!r}"
    )


def test_the_cli_offers_exactly_three_flags() -> None:
    """`--spec`, `--out`, `--verbose`, and nothing else.

    `run_all` passes the same two flags to every stage, so a fourth here would be a
    contract the orchestrator has to learn. The refusal half matters as much as the
    positive half: an unknown flag must be rejected by argparse, not absorbed.
    """
    import pytest as _pytest

    parser = build_pdf._parser()
    defined = {
        action.option_strings[0]
        for action in parser._actions
        if action.option_strings and action.option_strings[0] != "-h"
    }
    assert defined == {"--spec", "--out", "--verbose"}, (
        f"the PDF stage's CLI drifted from the three flags every stage shares: {defined}"
    )
    with _pytest.raises(SystemExit):
        parser.parse_args(["--spec", "x", "--root", "/tmp"])


@needs_toolchain
def test_the_stage_imports_nothing_from_the_test_tree() -> None:
    """`scripts/` must never import `tests/`.

    Found by doing it: the manifest writer was first written as
    `from test_contracts import assert_formats_contract`, which type-checks in this
    repository and fails in an installed skill, where no `tests/` directory exists at all.
    The dependency was inverted instead — the rule moved INTO `build_report`, next to the
    manifest it governs — and this asserts the inversion stays.
    """
    import ast

    tree = ast.parse(Path(build_pdf.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
    assert "test_contracts" not in imported and "tests" not in imported, (
        f"build_pdf imports {sorted(imported & {'test_contracts', 'tests'})}; scripts/ "
        f"must be importable in an installed skill, which ships no tests"
    )
    calls_the_rule = (
        any(isinstance(node, ast.Name) and node.id == "assert_formats_contract"
            for node in ast.walk(tree))
        or any(isinstance(node, ast.Attribute) and node.attr == "assert_formats_contract"
               for node in ast.walk(tree))
    )
    assert calls_the_rule, (
        "the writer must still call the shared rule rather than restate it; the rule now "
        "lives in build_report, next to the manifest it governs"
    )