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
    referenced = {row[0] for row in FAIL_ROWS} | {CLEAN_ANSWER}
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
