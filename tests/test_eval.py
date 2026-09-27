"""EVAL-01: the harness that grades a cheap model's answer, and its scope.

SCOPE, PRECISELY. This module tests the HARNESS. The live cheap-model run is
the repository owner's step, the runbook is in `README.md` (because
`references/` is closed at four files by 07-02's decision), and therefore
EVAL-01 closes **`partial` by design rather than by omission**. Nothing in this
repository calls a model, holds a key, or touches the network - and two tests
here prove that structurally rather than by promise.

`tools/` is on `sys.path` for this module only, by a two-line shim mirroring
what `conftest.py` does for `scripts/`. The validator imports nothing FROM the
skill, which is the whole reason this shim can be local instead of global.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

# `tools/` is NOT on the path by default: it is an owner-facing directory, not
# the stage surface `conftest.py` prepares. This shim is local to the one test
# module that needs it, mirroring the `scripts/` shim's comment about intent.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import validate_answer  # noqa: E402  (after the shim — deliberate)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
EVAL_DIR = FIXTURES_DIR / "eval"

# (answer fixture, metrics fixture, the ONE property it breaks). The low-
# confidence row is graded against a different metrics document on purpose:
# `metrics.example.json` has no `low` series, so property 2 would have nothing
# to look at and the row would pass vacuously.
FAIL_ROWS = (
    ("fail-novel-number.example.md", "metrics.example.json", "numbers_present"),
    (
        "fail-low-confidence.example.md",
        "metrics.low-confidence.example.json",
        "low_is_hypothesis",
    ),
    ("fail-missing-as-of.example.md", "metrics.example.json", "as_of_present"),
    ("fail-missing-sections.example.md", "metrics.example.json", "sources_and_limitations"),
)

CLEAN_ANSWER = "pass.example.md"
CLEAN_METRICS = "metrics.example.json"


def _check(answer_name: str, metrics_name: str, tmp_path: Path, capsys) -> tuple[int, str, Path]:
    """Grade one answer against one metrics document, entirely under `tmp_path`.

    The metrics document is COPIED to `tmp_path/out/metrics.json` rather than
    read from the fixtures directory: the validator writes its regression stub
    into the `--out` directory it is given, and pointing it at
    `tests/fixtures/` would drop a generated file into the committed tree on
    every run. This is the same reason `test_run_all` repoints
    `common.CACHE_DIR`.
    """
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "metrics.json").write_bytes((FIXTURES_DIR / metrics_name).read_bytes())
    capsys.readouterr()
    code = validate_answer.main(
        ["--answer", str(EVAL_DIR / answer_name), "--out", str(out_dir)]
    )
    return code, capsys.readouterr().out, out_dir


def test_a_clean_answer_passes_every_property(tmp_path: Path, capsys) -> None:
    """A well-formed answer passes all four properties and writes NO stub.

    The stub assertion is the half that matters most: a validator that wrote a
    regression fixture on success would fill `out/eval/regression/` with noise
    on every good run, and nobody would notice until the directory was huge.
    """
    code, output, out_dir = _check(CLEAN_ANSWER, CLEAN_METRICS, tmp_path, capsys)
    assert code == 0, output
    lines = [line for line in output.splitlines() if line.startswith(("PASS ", "FAIL "))]
    assert [line.split()[1] for line in lines] == list(validate_answer.PROPERTIES)
    assert all(line.startswith("PASS ") for line in lines), output
    assert not (out_dir / "eval" / "regression").exists()


def test_each_failing_fixture_fails_exactly_its_own_property(tmp_path: Path, capsys) -> None:
    """A four-row matrix: each fixture breaks its own property and no other.

    The "no other" half is the interesting one. A fixture that quietly broke
    two properties would still satisfy a test that only checked the expected
    failure, and the property it broke second would never get its own fixture.
    """
    for answer_name, metrics_name, expected in FAIL_ROWS:
        case = tmp_path / answer_name.replace(".example.md", "")
        case.mkdir()
        code, output, out_dir = _check(answer_name, metrics_name, case, capsys)

        assert code == 1, f"{answer_name}: expected exit 1, got {code}\n{output}"
        assert f"FAIL {expected}:" in output, f"{answer_name} did not fail {expected}\n{output}"
        for other in validate_answer.PROPERTIES:
            if other == expected:
                continue
            assert f"FAIL {other}" not in output, (
                f"{answer_name} broke {other} as well as {expected}\n{output}"
            )
            assert f"PASS {other}" in output, f"{answer_name} did not pass {other}\n{output}"
        stubs = sorted((out_dir / "eval" / "regression").glob("*.json"))
        assert [stub.name for stub in stubs] == [f"{expected}.example.json"], (
            f"{answer_name} wrote {len(stubs)} stub(s), expected exactly one"
        )


def test_a_failure_stub_records_the_metrics_it_was_checked_against(tmp_path: Path, capsys) -> None:
    """The stub is tied to the document it graded, or it is not a regression fixture.

    `metrics_sha256` is the binding field: a fixture promoted to
    `tests/fixtures/eval/` months later must say which metrics document produced
    the failure, or nobody can tell whether it still reproduces.
    """
    code, _output, out_dir = _check(
        "fail-novel-number.example.md", CLEAN_METRICS, tmp_path, capsys    )
    assert code == 1
    stub_path = out_dir / "eval" / "regression" / "numbers_present.example.json"
    stub = json.loads(stub_path.read_text(encoding="utf-8"))

    for key in validate_answer.STUB_KEYS:
        assert key in stub, f"the stub is missing {key!r}"
    assert stub["version"] == validate_answer.VERSION
    assert stub["property"] == "numbers_present"
    assert stub["offending_token"] == "+41.2%"
    assert stub["answer_filename"] == "fail-novel-number.example.md"
    assert stub["answer_excerpt"]
    assert stub["metrics_sha256"] == hashlib.sha256(
        (out_dir / "metrics.json").read_bytes()
    ).hexdigest()
    assert stub["answer_sha256"] == hashlib.sha256(
        (EVAL_DIR / "fail-novel-number.example.md").read_bytes()
    ).hexdigest()


def test_a_low_confidence_document_is_actually_checked(tmp_path: Path, capsys) -> None:
    """The vacuity guard: the graded document really does contain a `low` series.

    Without this, the property-2 row of the matrix would pass whether or not the
    check did anything - a metrics fixture that lost its `low` series would turn
    the row into "no low-confidence series to check" and still exit 1 for some
    other reason. The excerpt assertion is the second half: a failure that
    reports nothing is not a usable regression fixture.
    """
    document = json.loads((FIXTURES_DIR / "metrics.low-confidence.example.json").read_text(encoding="utf-8"))
    low_series = [s for s in document["series"] if s.get("confidence") == "low"]
    assert low_series, "the low-confidence fixture has no low series to check"

    code, _output, out_dir = _check(
        "fail-low-confidence.example.md", "metrics.low-confidence.example.json", tmp_path, capsys    )
    assert code == 1
    stub = json.loads(
        (out_dir / "eval" / "regression" / "low_is_hypothesis.example.json").read_text(
            encoding="utf-8"
        )
    )
    assert stub["answer_excerpt"], "the property-2 failure reported an empty excerpt"
    assert low_series[0]["series_id"] in stub["detail"]


def test_a_vacuous_low_check_says_so(tmp_path: Path, capsys) -> None:
    """A vacuous pass is STATED, not silent.

    Graded against `metrics.example.json`, which has no `low` series: the
    property passes, and the output says WHY it passed. A silent pass here is
    indistinguishable from a real check, which is the failure mode 06-04 kept
    insisting on.
    """
    code, output, _out_dir = _check(CLEAN_ANSWER, CLEAN_METRICS, tmp_path, capsys)
    assert code == 0
    assert "PASS low_is_hypothesis" in output
    assert "no low-confidence series to check" in output


def test_the_validator_module_is_stdlib_only_and_reads_no_network() -> None:
    """No HTTP client, no SDK, no key read, and no import from the skill.

    The positive half matters as much as the negative: the module DOES import
    `json` and `re`, so a file that never parsed anything cannot satisfy this
    test vacuously.
    """
    source = Path(str(validate_answer.__file__)).read_text(encoding="utf-8")

    for forbidden in (
        "urllib",
        "http.client",
        "socket",
        "requests",
        "httpx",
        "subprocess",
        "api_key",
        "API_KEY",
        "openai",
        "anthropic",
    ):
        assert forbidden not in source, f"validate_answer.py must not contain {forbidden!r}"

    for skill_module in ("common", "analyze_trends", "make_charts", "build_report", "resolve_articles"):
        assert f"import {skill_module}" not in source, (
            f"validate_answer.py must not import the skill module {skill_module!r}"
        )

    assert "import json" in source
    assert "import re" in source


def test_the_eval_suite_never_reaches_the_network(tmp_path: Path) -> None:
    """A coarse, honest second check on the same property.

    No function is NAMED like a request. This is deliberately blunt and is
    labelled as such: the structural check above (no `urllib`, no `socket`, no
    `subprocess`, no SDK) is the real one, and this catches the case where a
    future edit adds `def fetch_json(...)` on top of an innocent import list.
    """
    import ast

    tree = ast.parse(Path(str(validate_answer.__file__)).read_text(encoding="utf-8"))
    suspicious = {"fetch", "get", "post", "request", "call", "complete", "download"}
    defined: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
    offenders = sorted(name for name in defined if name.lower() in suspicious)
    assert not offenders, f"validate_answer.py defines request-shaped functions: {offenders}"
    assert defined, "the module defines no functions, so this check proves nothing"


def test_every_eval_fixture_is_referenced_by_a_test() -> None:
    """An orphan fixture rots; a referenced one is a regression test.

    The named set is derived from `FAIL_ROWS` plus the clean answer, so adding a
    fixture without wiring it into the matrix is a failing test rather than a
    file nobody reads.
    """
    referenced = {row[0] for row in FAIL_ROWS} | {CLEAN_ANSWER, DECLINING_ANSWER}
    on_disk = {path.name for path in EVAL_DIR.glob("*.md")}
    assert on_disk, "tests/fixtures/eval/ is empty, so this proves nothing"
    orphans = sorted(on_disk - referenced)
    assert not orphans, f"no test grades these fixtures: {orphans}"
    missing = sorted(referenced - on_disk)
    assert not missing, f"the matrix names fixtures that do not exist: {missing}"


def test_a_missing_input_is_a_usage_problem_not_an_answer_problem(tmp_path: Path) -> None:
    """Exit 2, never 1: the tool's own problems are distinguishable.

    Without this split, a wrong path and a wrong answer would look identical to
    whatever automation reads the exit code, and the first thing an owner sees
    would be a stub blaming a model that was never asked.
    """
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "metrics.json").write_bytes((FIXTURES_DIR / CLEAN_METRICS).read_bytes())

    assert validate_answer.main(["--answer", str(tmp_path / "nope.md"), "--out", str(out_dir)]) == 2
    assert validate_answer.main(["--answer", str(EVAL_DIR / CLEAN_ANSWER), "--out", str(tmp_path / "void")]) == 2
    # No stub is written for a usage problem either.
    assert not (out_dir / "eval").exists()


# ---------------------------------------------------------------------------
# FINDING-04 (bug.md): a negative metrics value had no quotable spelling.
#
# `_normalise` stripped a leading `-` while `_accepted_forms` emitted only
# signed spellings, so the value's own form was never in the accepted set and
# no answer could quote a decline. Three neighbouring defects had the same
# root - the tokeniser could not read the spellings a Ukrainian answer uses.
#
# The committed `metrics.example.json` that the five pre-existing fixtures are
# written against contains NO negative value, which is why none of them could
# catch this. `metrics.declining.example.json` is the real intermittent-fasting
# run: y1 clean -38.5% and -44.5%, both series `low`.
# ---------------------------------------------------------------------------

DECLINING_ANSWER = "pass.declining-growth.example.md"
DECLINING_METRICS = "metrics.declining.example.json"


def test_a_declining_series_has_an_answer_that_passes(tmp_path: Path, capsys) -> None:
    """The skill's own documented example, passing the skill's own check.

    This is the whole point of the fix. Before it, every declining series had
    NO valid answer: quoting the correct number and passing property 1 were
    mutually exclusive, so `manual.md` A.8 failed its own validator.
    """
    code, output, out_dir = _check(DECLINING_ANSWER, DECLINING_METRICS, tmp_path, capsys)
    assert code == 0, output
    assert all(
        line.startswith("PASS ") for line in output.splitlines() if line[:5] in ("PASS ", "FAIL ")
    ), output
    assert not (out_dir / "eval" / "regression").exists()


def test_the_declining_run_checks_property_two_non_vacuously(tmp_path: Path, capsys) -> None:
    """Both series in this document are `low`, so property 2 has real work.

    `test.md` section 11 recorded `low_is_hypothesis` passing twice on an
    EMPTY check (`NOTE low_is_hypothesis: no low-confidence series to check`).
    A harness whose strongest property never fires is a harness nobody has
    tested, so the same document is used to prove property 2 does fire.
    """
    metrics = json.loads((FIXTURES_DIR / DECLINING_METRICS).read_text(encoding="utf-8"))
    assert [series["confidence"] for series in metrics["series"]] == ["low", "low"]
    code, output, _ = _check(DECLINING_ANSWER, DECLINING_METRICS, tmp_path, capsys)
    assert code == 0, output
    assert "no low-confidence series to check" not in output, (
        "the vacuous-PASS note must not appear for a document with two low series"
    )


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        ("-38.5", -38.5),  # ASCII minus, the honest signed form
        ("\u221238.5", -38.5),  # U+2212, what a word processor produces
        ("-38,5", -38.5),  # comma decimal separator
        ("\u221238,5", -38.5),  # both at once - exactly what manual.md A.8 writes
        ("+14.5", 14.5),  # the control that must survive the fix
    ],
    ids=["ascii_minus", "typographic_minus", "comma_decimal", "both", "positive_control"],
)
def test_every_legal_spelling_of_a_decline_is_accepted(
    spelling: str, expected: float
) -> None:
    """One source value, the spellings a person actually types."""
    assert validate_answer._normalise(spelling) == pytest.approx(expected)


def test_an_unsigned_decline_is_rejected_and_that_is_deliberate() -> None:
    """`38.5%` is NOT a legal spelling of a source value of -38.5.

    This is a deliberate refusal, not an oversight. A bare `38.5` and a `+38.5`
    are the SAME token to this checker, so accepting the unsigned magnitude of a
    negative value would let an answer claim a 38.5% INCREASE against a source
    of -38.5 - the exact sign error property 1 exists to catch, and one no
    surrounding-word analysis can distinguish here. So a decline is quoted WITH
    its sign, which is what `manual.md` A.8 already does.
    """
    assert "-38.5" in validate_answer._accepted_forms(-38.5)
    assert "\u221238,5" not in validate_answer._accepted_forms(-38.5)
    metrics = json.loads((FIXTURES_DIR / DECLINING_METRICS).read_text(encoding="utf-8"))
    assert validate_answer.check_numbers("growth -38.5%", metrics).passed
    unsigned = validate_answer.check_numbers("growth 38.5%", metrics)
    assert not unsigned.passed, (
        "an unsigned decline must not pass, or a sign error is undetectable"
    )
    assert validate_answer.check_numbers("growth +38.5%", metrics).passed is False



def test_a_comma_is_a_decimal_separator_only_when_it_cannot_be_a_thousands_group() -> None:
    """`52,5` is 52.5. `1,720` is 1720. Getting this backwards invents numbers."""
    assert validate_answer._normalise("52,5") == pytest.approx(52.5)
    assert validate_answer._normalise("1,720") == pytest.approx(1720)
    assert validate_answer._normalise("1,720,628") == pytest.approx(1720628)
    assert validate_answer._normalise("1,234.5") == pytest.approx(1234.5)


def test_the_sign_is_compared_and_not_discarded() -> None:
    """A negative value must not be satisfied by a positive spelling, or vice versa."""
    assert validate_answer._normalise("-52.5") == pytest.approx(-52.5)
    assert validate_answer._normalise("52.5") == pytest.approx(52.5)
    assert validate_answer._normalise("+14.5") == pytest.approx(14.5)
    # `-52.5` and `52.5` are DIFFERENT values, so neither accepted set may
    # contain the other.
    negative = {validate_answer._normalise(f) for f in validate_answer._accepted_forms(-52.5)}
    positive = {validate_answer._normalise(f) for f in validate_answer._accepted_forms(52.5)}
    assert 52.5 not in negative, "a negative value accepted its own unsigned positive"
    assert -52.5 not in positive, "a positive value accepted a negative spelling"


def test_digits_inside_a_proper_name_are_exempt_but_a_standalone_number_is_not() -> None:
    """`N8n` must be nameable; `8` standing alone must still be checked.

    The exemption is a LETTER on both sides, so it cannot reach a measurement
    in prose - the second half of this test is what proves that, and it is the
    half that would be missing if the rule were "any alphabetic neighbour".
    """
    metrics = {"as_of": "2026-09-26", "series": []}
    named = validate_answer.check_numbers("The article N8n was measured.", metrics)
    assert not named.offending_token, (
        f"N8n must be nameable, but {named.offending_token!r} was read as a measurement"
    )
    standalone = validate_answer.check_numbers("The article had 8 views.", metrics)
    assert standalone.offending_token is not None
    assert standalone.offending_token.strip() == "8", (
        "a standalone number must still be checked; the letter-bounded rule "
        "must not have become 'any alphabetic neighbour'"
    )



def test_numbers_quoted_from_a_rubric_reason_string_are_a_source() -> None:
    """`period at least 91 days` is mandatory content, so 91 must be a source.

    SKILL.md requires the confidence reasons and the seasonality note to be
    quoted verbatim. Before FINDING-04 `_numeric_leaves` skipped every string,
    so an answer that obeyed that rule failed property 1 on the rubric's own
    thresholds.
    """
    metrics = json.loads((FIXTURES_DIR / DECLINING_METRICS).read_text(encoding="utf-8"))
    dates: set[str] = set()
    pool = validate_answer._numeric_leaves(metrics, dates)
    for threshold in (91, 30, 1000, 5):
        assert float(threshold) in pool, (
            f"{threshold} appears only inside a confidence_reasons string and "
            "must still be quotable"
        )


def test_a_reason_string_cannot_inject_a_decimal_a_sign_or_a_percent() -> None:
    """The reason scan is a bare integer, deliberately narrower than the token regex.

    Without this, any string in `metrics.json` could add a value to the source
    set that the tool would not itself have produced.
    """
    forged = {
        "as_of": "2026-09-26",
        "series": [
            {
                "series_id": "forged",
                "confidence_reasons": ["-1234.5%", "0.1", "+7"],
                "seasonality": {"note": "9999999"},
            }
        ],
    }
    dates: set[str] = set()
    pool = validate_answer._numeric_leaves(forged, dates)
    assert -1234.5 not in pool
    assert 0.1 not in pool
    assert 7.0 in pool, "a bare integer in a reason string is a legitimate source"
    assert 9999999.0 in pool


def test_an_invented_number_still_fails_and_there_is_still_no_tolerance_band(
    tmp_path: Path, capsys
) -> None:
    """Widening the accepted SPELLINGS must not widen the accepted VALUES.

    The closed-set property is the whole reason property 1 is a mechanical
    claim rather than a promise. `4247.7` is a real growth figure in
    `out-ai-automation`; `4247.9` is not, and a 0.2% band would let it through.
    """
    metrics = json.loads(
        (FIXTURES_DIR / "metrics.declining.example.json").read_text(encoding="utf-8")
    )
    for invented in ("-38.6%", "-38.4%", "-99.9%", "-38,6%", "38.5%", "+38.5%"):
        result = validate_answer.check_numbers(f"growth {invented}", metrics)
        assert not result.passed, f"{invented} was accepted; the check has a band"
    for real in ("-38.5%", "\u221238.5%", "-38,5%"):
        result = validate_answer.check_numbers(f"growth {real}", metrics)
        assert result.passed, f"{real} was rejected but is a real spelling"

