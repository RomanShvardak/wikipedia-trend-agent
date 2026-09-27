"""PUB-01: the tree is honest about what it is, and a clone of it works.

WHAT THIS MODULE IS. The mechanical half of the publication gate. A README that
has merely stopped being stale but says nothing is a failure, so the section
presence is asserted; a README whose worked example was typed by hand is a
failure, so the example is compared byte-for-byte against a REAL render
produced by the shipped stages on every test run; a truncated licence paste is a
failure, so the licence is measured; and a `.gitignore` that has drifted from
what the tree actually generates is a failure, so the file is read off disk.

WHAT THIS MODULE DELIBERATELY DOES NOT DO. It does not run `pip install`. The
clean-clone rehearsal has two halves and the split is the point: the FILE-SET
half runs here, with no network, as a real gate; the INSTALL half is one
documented owner command (`python tools/clean_clone_check.py --full`), because
a real install inside a unit test is minutes of network and would make this
suite non-zero-network. It also does not assert the absence of a git remote: a
remote appearing is the goal, and such a test would fail on success. The remote
question is settled by running `git remote -v` and quoting the output, which
the SUMMARY does.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

# `tools/` is on the path HERE, locally, and not by relying on `test_eval.py`
# having inserted it: pytest collects modules alphabetically, and a test module
# that silently depends on another module's import side effect breaks the moment
# one of them is deselected or renamed. Same two-line shape as the `scripts/`
# shim in conftest.py, and for the same reason.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import build_report
import clean_clone_check
import make_charts
import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
README = SKILL_DIR / "README.md"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# The two committed chart-stage inputs, mapped onto the FIXED names the stage
# reads. `test_report._copy_chart_inputs` documents why: a test that "just
# worked" with `series.example.csv` in place would be proving nothing, because
# the stage would have failed on a missing `series.csv`.
CHART_INPUTS = (
    ("series.example.csv", "series.csv"),
    ("metrics.example.json", "metrics.json"),
)

# The generated paths the .gitignore must exclude. Nothing beyond this list is
# asserted: an over-strict ignore test is a nuisance, not a gate.
#
# `result` LEFT this list with the owner's revised ruling of 2026-09-27: a run folder is
# the skill's public evidence, so it is published. The run folder still lives under
# `result/` and nowhere else - that half of the ruling is unchanged, and it is what makes
# publication reviewable instead of scattering artifacts at the skill root. The
# never-committed half now belongs to `out/`, which is the `--out` scratch default:
# `test_result_is_published_not_ignored` holds both ends.
REQUIRED_IGNORES = frozenset(
    {"out", ".cache", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache"}
)

# A license file that is not the license is worse than none, so the checks are
# on the canonical landmarks, not on a paraphrase. The canonical Apache-2.0
# text is 11358 bytes; a truncated paste is well under 9000, so the floor sits
# just below the real thing.
LICENSE_MIN_BYTES = 9000


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _example_block(readme: str) -> str:
    """The README's worked-example fenced block, the one labelled `out/report.md`."""
    marker = "`out/report.md`:"
    start = readme.index(marker)
    opening = readme.index("```markdown", start)
    closing = readme.index("```", opening + len("```markdown"))
    return readme[opening + len("```markdown") : closing]


def _render_report(out_dir: Path) -> str:
    """Run the REAL chart stage then the REAL report stage, and return report.md.

    The document is read OFF DISK, never from `render_report`'s return value, for
    the reason `test_report._render_two_series` documents: a fidelity check that
    reads the planner's output would prove the planner formats what it intends,
    while the published file is what a reader opens.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    for source, target in CHART_INPUTS:
        out_dir.joinpath(target).write_bytes((FIXTURES_DIR / source).read_bytes())
    spec = str(FIXTURES_DIR / "spec.example.json")
    assert make_charts.main(["--spec", spec, "--out", str(out_dir)]) == 0
    assert build_report.main(["--spec", spec, "--out", str(out_dir)]) == 0
    return out_dir.joinpath(build_report.REPORT_FILENAME).read_text(encoding="utf-8")


def test_the_readme_is_not_the_phase_one_stub() -> None:
    """The stale status line is gone AND the README says something.

    A README that stopped being stale but says nothing is the failure this
    guards: the absence assertion alone would be satisfied by an empty file.
    """
    readme = _read(README)
    assert "Phase 1 skeleton" not in readme, "the stale Phase 1 status line is back"

    assert "python scripts/run_all.py --spec" in readme
    for flag in ("--spec", "--out", "--verbose"):
        assert flag in readme, f"the one-command block does not document {flag}"

    for section in (
        "## The pipeline",
        "## Install",
        "## The one command",
        "## Worked example",
        "## The honesty contract",
        "## Environment variables",
        "## Cheap-model eval",
        "## Develop it further",
        "## Sources and data",
        "## Status",
    ):
        assert section in readme, f"the README is missing the {section!r} section"


def test_the_readme_quotes_a_real_render(tmp_path: Path) -> None:
    """The worked example is REAL output, not hand-written.

    This is what makes "not hand-written" mechanical rather than a promise: the
    report is re-rendered by the shipped stages on every test run, and every
    non-blank line of the README's excerpt must be a substring of it. The first
    missing line is reported verbatim, so the fix is obvious.
    """
    readme = _read(README)
    block = _example_block(readme)
    rendered = _render_report(tmp_path / "out")

    quoted = [line for line in block.splitlines() if line.strip()]
    assert len(quoted) >= 8, f"the excerpt is only {len(quoted)} non-blank lines"

    for line in quoted:
        assert line in rendered, (
            "this README line is not in the real render, so the example has drifted "
            f"or was typed by hand:\n  {line!r}"
        )


def _metric_leaves(node: object, out: set[str]) -> None:
    """Every numeric leaf of the metrics document, as a normalised string."""
    if isinstance(node, dict):
        for value in node.values():
            _metric_leaves(value, out)
    elif isinstance(node, list):
        for item in node:
            _metric_leaves(item, out)
    elif isinstance(node, bool):
        return
    elif isinstance(node, (int, float)):
        value = float(node)
        out.add(str(value))
        out.add(f"{value:,.1f}")
        out.add(f"{value:,}")
        out.add(f"{value:,.0f}")
        if value.is_integer():
            out.add(str(int(value)))
        out.add(str(round(value, 0)))
        out.add(str(round(value, 1)))


def test_every_number_the_readme_quotes_is_in_the_metrics_fixture() -> None:
    """The excerpt's numbers are metrics values, checked independently of the eval tool.

    The normalisation reasoning is the same one `tools/validate_answer.py`
    documents, deliberately re-implemented here: the publish tests must not
    depend on the eval tool, so a bug in one cannot hide a bug in the other.
    Date components and table-label digits are exempt with the same stated
    reason - a date is not a measurement.
    """
    metrics = json.loads((FIXTURES_DIR / "metrics.example.json").read_text(encoding="utf-8"))
    dates: set[str] = set()
    for series in metrics["series"]:
        dates.add(series["period"]["start"])
        dates.add(series["period"]["end"])
        for window in series["growth"].values():
            dates.add(window["start"])
            dates.add(window["end"])
    dates.add(metrics["as_of"])

    accepted: set[str] = set()
    _metric_leaves(metrics, accepted)
    accepted |= {date.replace("-", "") for date in dates}

    number_pattern = re.compile(r"[+-]?\d[\d, ]*(?:\.\d+)?%?")
    # A WINDOW LABEL, not a measurement: the report writes `3M`, `1Y` and `2Y`
    # as the names of the growth windows, and the digit in `3M` is a duration
    # the reader already knows, not a value the pipeline computed. The metrics
    # keys are `m3`/`y1`/`y2`, so the digit carries no information here.
    window_label = re.compile(r"^\d[MY]$")
    iso_date = re.compile(r"\d{4}-\d{2}-\d{2}")
    # A percent-ENCODED slug is spec content, not a measurement: CONTRACTS.md §1
    # carries the Czech article verbatim as `P%C5%AFst_p%C5%99eru%C5%A1ovan%C3%BD`.
    # The same reasoning as the validator's, re-implemented here on purpose so a
    # bug in one exemption cannot hide a bug in the other.
    encoding_triplet = re.compile(r"%[0-9A-Fa-f]{2}")
    # A localized CONFIDENCE REASON is a WORD, not a displayed metric. The
    # report's trust bullet quotes the reason phrases the report stage chose -
    # "період щонайменше 91 день; переглядів за 30 днів щонайменше 10000" - and
    # the digits inside those phrases are the rubric's constants, which are not
    # leaves of metrics.json. CONTRACTS.md §8.4 governs DISPLAYED numbers; a
    # reason phrase is prose the reader reads, and §8.3 already says so. The
    # membership test uses the real token table rather than a hand-kept list of
    # phrases, so a new reason is covered the moment it is added.
    reason_phrases = tuple(build_report.REASON_TOKENS["uk"].values())
    checked = 0
    reason_lines = 0
    for line in _example_block(_read(README)).splitlines():
        if not line.strip():
            continue
        if any(phrase in line for phrase in reason_phrases):
            reason_lines += 1
            continue
        # A date COMPONENT is exempt wherever it appears, not only on a line
        # that is nothing but a date: `2026` inside `2026-09-20` is a year, not
        # a pageview count. Decided by span overlap, exactly as the validator
        # decides it, so the two agree on what a date is.
        date_spans = [(match.start(), match.end()) for match in iso_date.finditer(line)]
        encoding_spans = [
            (match.start(), match.end()) for match in encoding_triplet.finditer(line)
        ]
        for match in number_pattern.finditer(line):
            token = match.group(0)
            bare = token.lstrip("+-").rstrip("%").replace(",", "").replace(" ", "")
            if any(match.start() < end and start < match.end() for start, end in date_spans):
                continue
            if any(match.start() < end and start < match.end() for start, end in encoding_spans):
                continue
            if window_label.match(line[match.start() : match.end() + 1]):
                continue
            if bare in {date.replace("-", "") for date in dates}:
                continue
            assert bare in accepted or token.lstrip("+-").rstrip("%") in accepted, (
                f"the README quotes {token!r}, which is not a value in "
                f"metrics.example.json (line: {line.strip()[:120]!r})"
            )
            checked += 1
    # Non-vacuity, in both directions. Eight is the number of displayed metrics
    # in the excerpt after the four stated exemptions (date components, window
    # labels, percent-encoded slugs, localized reason phrases) - it is a
    # measured floor, not a guess. And the reason-phrase exemption is itself
    # counted, so it cannot be widened until it swallows the whole excerpt and
    # the check quietly stops checking anything.
    assert checked >= 8, f"only {checked} numbers were checked, so this is nearly vacuous"
    assert reason_lines >= 2, (
        f"only {reason_lines} line(s) were exempted as localized reason phrases; the "
        f"exemption should be reaching the excerpt's trust bullets"
    )


def test_the_licence_is_the_complete_apache_two() -> None:
    """The LICENSE is the licence, complete, with a named copyright holder."""
    licence = SKILL_DIR / "LICENSE"
    assert licence.is_file(), "there is no LICENSE file"
    text = _read(licence)

    assert "Apache License" in text[:200], "the file does not open with the Apache header"
    assert "Version 2.0, January 2004" in text
    assert "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in text
    assert "END OF TERMS AND CONDITIONS" in text
    assert "APPENDIX: How to apply the Apache License to your work" in text
    assert "http://www.apache.org/licenses/LICENSE-2.0" in text

    size = licence.stat().st_size
    assert size > LICENSE_MIN_BYTES, (
        f"LICENSE is {size} bytes; a truncated paste is well under {LICENSE_MIN_BYTES}"
    )

    holder = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Copyright ") and "[" not in stripped:
            holder = stripped[len("Copyright ") :].strip()
            break
    assert holder, "no filled-in Copyright line naming a holder"
    assert holder.replace(" ", ""), "the Copyright line names an empty holder"


def test_gitignore_covers_every_generated_path() -> None:
    """The ignore file is READ, not asserted in prose.

    Both caches this repository actually generates were missing from the file
    and are present in the working tree right now, which is exactly the class of
    leak PUB-01 exists to close.
    """
    entries = set()
    for line in _read(SKILL_DIR / ".gitignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entries.add(stripped.rstrip("/"))
    missing = sorted(REQUIRED_IGNORES - entries)
    assert not missing, f".gitignore does not exclude: {missing}"


def test_the_repository_is_not_published_by_this_plan() -> None:
    """What IS this plan's business: no credential-shaped file, one dependency.

    The absence of a git remote is deliberately NOT asserted here. A remote
    appearing is the goal of publication, so a test asserting its absence would
    fail on success; that question is settled by running `git remote -v` and
    quoting the output in the SUMMARY.
    """
    suspicious = [
        path.relative_to(SKILL_DIR).as_posix()
        for path in SKILL_DIR.rglob("*")
        if path.is_file() and (path.name.endswith(".env") or path.name.startswith(".env"))
    ]
    assert not suspicious, f"credential-shaped files in the tree: {suspicious}"

    requirements = _read(SKILL_DIR / "requirements.txt")
    lines = [line.strip() for line in requirements.splitlines() if line.strip()]
    assert lines == ["matplotlib>=3.11"], (
        f"the runtime dependency budget changed: {lines}"
    )


def test_the_readme_does_not_promise_a_network_free_install() -> None:
    """The README must not claim the install half of the rehearsal is automated.

    The split between the two rehearsal halves is a scope decision, and a README
    that implied `pytest` performs a real install would be making a claim the
    suite does not keep.
    """
    readme = _read(README)
    assert "clean_clone_check.py --full" in readme, (
        "the README does not document the owner-side rehearsal command"
    )
    tree = ast.parse(_read(SKILL_DIR / "tools" / "clean_clone_check.py"))
    docstring = ast.get_docstring(tree) or ""
    assert "pip install" in docstring, "the rehearsal tool does not document its install step"
    assert "pytest" in docstring

# Every file a clone must carry, as relative POSIX paths. Asserted as a
# SUPERSET, not an equality, so adding a legitimately-needed file later does not
# force an edit here.
EXPECTED_CLONE_FILES = (
    "SKILL.md",
    "README.md",
    "LICENSE",
    ".gitignore",
    "requirements.txt",
    "pyproject.toml",
    "scripts/common.py",
    "scripts/fetch_pageviews.py",
    "scripts/analyze_trends.py",
    "scripts/make_charts.py",
    "scripts/build_report.py",
    "scripts/resolve_articles.py",
    "scripts/run_all.py",
    "tools/validate_answer.py",
    "tools/clean_clone_check.py",
    "references/CONTRACTS.md",
    "references/INTERPRETATION.md",
    "references/API_ACCESS.md",
    "references/DATA_CAVEATS.md",
    "assets/example.intermittent-fasting.json",
    "assets/wikipedia-projects.json",
)


def _copy_clone(dest: Path) -> tuple[int, str]:
    """Run the rehearsal in its default, no-network mode; return its output."""
    code = clean_clone_check.main(["--check-only", "--dest", str(dest)])
    assert code == 0, "the clean-clone copy mode failed"
    return code, ""


def test_the_clean_clone_file_set_excludes_every_generated_path(tmp_path: Path) -> None:
    """The copy carries no generated path, and the SOURCE is left byte-identical.

    The `CHANGED` half is the one that is usually assumed rather than proven: a
    copy helper that resolved a path against the wrong root would happily
    "succeed" while having moved the repository's own files around. So the
    source file count and the `.gitignore` bytes are compared before and after.
    """
    def source_state() -> tuple[int, bytes]:
        return (
            sum(1 for path in SKILL_DIR.rglob("*") if path.is_file()),
            (SKILL_DIR / ".gitignore").read_bytes(),
        )

    before = source_state()
    _copy_clone(tmp_path / "clone")
    assert source_state() == before, "the copy mutated the source tree"

    copied = {
        path.relative_to(tmp_path / "clone").as_posix()
        for path in (tmp_path / "clone").rglob("*")
        if path.is_file()
    }
    assert copied, "the copy is empty, so this proves nothing"
    offenders = sorted(
        relative
        for relative in copied
        if any(part in clean_clone_check.FORBIDDEN_SEGMENTS for part in Path(relative).parts)
        or Path(relative).name.endswith(clean_clone_check.FORBIDDEN_SUFFIXES)
    )
    assert not offenders, f"the copy carries generated paths: {offenders}"


def test_the_cloned_tree_carries_every_file_a_clone_needs(tmp_path: Path) -> None:
    """Nothing a stranger needs is lost by the copy, and the naming rule survives it.

    The `name` == directory-name assertion is the agentskills.io rule tested
    AFTER a copy, which is where it would actually bite: a clone that renamed
    the directory on the way through would load as nothing.
    """
    clone = tmp_path / "wikipedia-trend-agent"
    _copy_clone(clone)

    copied = {
        path.relative_to(clone).as_posix() for path in clone.rglob("*") if path.is_file()
    }
    missing = sorted(set(EXPECTED_CLONE_FILES) - copied)
    assert not missing, f"the clone is missing: {missing}"

    for stage in ("scripts", "tests"):
        for path in sorted((SKILL_DIR / stage).glob("*.py")):
            relative = f"{stage}/{path.name}"
            assert relative in copied, f"the clone is missing {relative}"
    for path in sorted((SKILL_DIR / "tests" / "fixtures").rglob("*")):
        if path.is_file():
            relative = path.relative_to(SKILL_DIR).as_posix()
            assert relative in copied, f"the clone is missing the fixture {relative}"

    name_line = next(
        line for line in (clone / "SKILL.md").read_text(encoding="utf-8").splitlines() if line.startswith("name:")
    )
    assert name_line.split(":", 1)[1].strip() == clone.name


def test_the_rehearsal_full_mode_is_documented_not_run() -> None:
    """The expensive half exists, is documented, and is deliberately not a unit test.

    This is the honest encoding of the scope decision. A real `pip install`
    inside a test is minutes of network and would break QA-01. A future phase
    may add a nightly job that runs `--full`; this suite will not, and this
    test is what makes that a stated decision rather than an oversight.
    """
    import inspect

    docstring = clean_clone_check.__doc__ or ""
    assert "pip install" in docstring, "the rehearsal tool does not document its install step"
    assert "pytest" in docstring

    source = Path(str(inspect.getfile(clean_clone_check))).read_text(encoding="utf-8")
    assert '"--full"' in source, "the --full mode is not defined at all"

    # AST, not a string search: the question is whether any test ACTUALLY passes
    # --full to main(), and a substring search over this file would match its own
    # assertion. Every call to the rehearsal tool in this module is found and
    # its argument list inspected.
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    calls: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "main"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "clean_clone_check"
        ):
            calls.append(ast.dump(node))
    assert calls, "no rehearsal call found, so this check proves nothing"
    for dumped in calls:
        assert "--full" not in dumped, (
            "a test in test_publish.py passes --full to the rehearsal tool; the install "
            "half is the owner's command and must never run inside the suite"
        )

# ---------------------------------------------------------------------------
# FINDING-03 (bug.md): the 256-code-point bound on `--reason` was undocumented.
#
# A ~500-character justification was refused with
#   projects[0].selection.reason must be bounded and control-free
# which names neither the number nor the remedy, and appears in no document. The
# reader had no way to learn the bound except by failing.
#
# A documentation claim with no gate behind it is the 06-01 failure mode, so
# these assertions make the docs load-bearing.
# ---------------------------------------------------------------------------

SKILL_ROOT = Path(__file__).resolve().parent.parent


def _doc(name: str) -> str:
    return (SKILL_ROOT / name).read_text(encoding="utf-8")


# The two operator documents that live under `result/` rather than the skill root.
#
# They were moved there with the rest of the published material, and the owner's revised
# ruling publishes `result/` - so they are still TRACKED and still gated, just at a
# different path. Listed as data rather than as a per-test `result/` prefix so that a
# later move fails here by name, in one place.
PUBLISHED_DOCUMENTS = {
    "manual.md": "result/manual.md",
    "desc.md": "result/desc.md",
}


def _document_path(name: str) -> Path:
    """Resolve a gated document to its published path.

    `Path(name)` is returned unchanged for the root documents; the two under
    `result/` are redirected through `PUBLISHED_DOCUMENTS`. Both halves must be
    TRACKED - that is the point of the gate - and `test_result_is_published_not_ignored`
    is what holds the `result/` half of the ruling.
    """
    return SKILL_ROOT / PUBLISHED_DOCUMENTS.get(name, name)


def _tracked_doc(name: str) -> str:
    """Read a document that MUST be in the repository, or fail loudly.

    Everything reachable through this helper is tracked, so a missing one is a
    real packaging defect and the test must say so rather than skip. That is the
    whole point: a documentation gate that silently stops running when its
    subject disappears is not a gate.
    """
    path = _document_path(name)
    assert path.is_file(), (
        f"{name} is not in the skill directory (looked for {path.name} at "
        f"{path.parent.name}/). The tracked operator documents are README.md, "
        "SKILL.md, result/manual.md, result/desc.md and references/*.md, and "
        "each one is gated by a test in this module."
    )
    return path.read_text(encoding="utf-8")



TRACKED_OPERATOR_DOCUMENTS = (
    "README.md",
    "SKILL.md",
    "manual.md",
    "desc.md",
    "references/CONTRACTS.md",
    "references/INTERPRETATION.md",
    "references/API_ACCESS.md",
    "references/DATA_CAVEATS.md",
)


def test_the_owners_private_test_reports_are_never_committed() -> None:
    """`bug.md` and `test.md` must not be tracked, and `.gitignore` must say why.

    Both are the owner's private 2026-09-27 simulation-test record. `test.md`
    line 35 carries a REAL personal email address in a `WTI_USER_AGENT` example,
    and this repository is PUBLIC. `bug.md` was committed once by mistake in
    Phase 8 and untracked in the next commit; this test is what stops it being
    re-added by a well-meaning `git add -A`.

    Asserted on `git ls-files`, not on the filesystem: the point is what a clone
    would RECEIVE, and a file can sit on disk forever without ever being tracked.
    The assertion is skipped when git is unavailable, because a packaging test
    that fails because VCS is missing teaches the reader nothing.
    """
    import subprocess

    def tracked(name: str) -> bool:
        return (
            subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", name],
                capture_output=True,
                text=True,
            ).returncode
            == 0
        )

    probe = subprocess.run(["git", "ls-files"], capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip("git is not available in this environment")

    published = [name for name in ("bug.md", "test.md") if tracked(name)]
    assert not published, (
        f"the owner's private test reports must never be published: {published}. "
        "Use `git rm --cached <file>` - do NOT delete them from disk, they are "
        "the source of truth for the Phase 8 verification."
    )

    # The RULES, not the file text. Phase 8 already learned this the hard way
    # with `assert "256" in manual`, which passed because the manual documented
    # 256 code points for an unrelated variable. Here the .gitignore COMMENT
    # names both files, so a substring search over the whole file is satisfied
    # by the explanation of the rule rather than by the rule - and deleting the
    # rules would leave the test green. Comment lines are dropped first.
    rules = [
        line.strip()
        for line in (SKILL_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    for name in ("bug.md", "test.md"):
        assert name in rules, (
            f"{name} is untracked but not an ignore RULE, so the next "
            f"`git add -A` re-adds it. Current rules: {rules}"
        )
    # The rule must carry its reason. A bare name in a .gitignore invites the
    # next reader to "clean it up" as an accident, and the accident is a
    # published personal email address.
    ignore_text = (SKILL_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "WTI_USER_AGENT" in ignore_text, (
        "the .gitignore comment must state WHY these are excluded, so nobody "
        "removes the rules as cruft"
    )


def test_every_tracked_operator_document_is_present_and_non_empty() -> None:
    """The set this module gates, asserted as a set.

    Phase 8 added documentation gates for `manual.md` and `API_ACCESS.md` while
    both `manual.md` and `desc.md` were still UNTRACKED - so the manual's gate
    could only skip, and a documentation test that silently stops running when
    its subject is not in the repository is not a gate. Tracking the documents
    closed that, and this test is what keeps it closed: a deleted or renamed
    operator document fails here by name, instead of quietly removing the
    assertions that read it.

    `EXPECTED_CLONE_FILES` above cannot do this job - it is a subset check over
    the files the clean-clone rehearsal copies, so a document can be absent from
    the repository and still pass it.
    """
    for name in TRACKED_OPERATOR_DOCUMENTS:
        path = _document_path(name)
        assert path.is_file(), f"tracked operator document is missing: {name} ({path})"
        text = path.read_text(encoding="utf-8")
        assert text.strip(), f"tracked operator document is empty: {name}"
        assert len(text) > 500, (
            f"{name} is {len(text)} characters; a truncated paste is not a "
            "document, and 07-03 already had to defend against exactly that "
            "for LICENSE"
        )


def test_the_manual_states_the_reason_bound_before_the_reader_hits_it() -> None:
    """`manual.md` §11 step 4, not just the error message.

    The assertion is scoped to the `--reason` ROW and the step-4 paragraph, not
    to the file. The manual already documented 256 code points for
    `WTI_USER_AGENT`, so a bare `assert "256" in manual` was satisfied before
    this plan wrote a word - a documentation gate that passes on an unrelated
    sentence is the 06-01 failure mode with extra steps, and the injected-defect
    probe caught exactly that.
    """
    manual = _tracked_doc("manual.md")

    lines = manual.splitlines()

    # The flag-table row, which is what a reader consults BEFORE running.
    reason_row = [line for line in lines if line.startswith("| `--reason")]
    assert reason_row, "the --reason flag row is gone from the manual's flag table"
    assert "256" in reason_row[0], (
        "the flag table must state the bound, because that table is what a "
        "reader consults before running the command"
    )

    # The step-4 paragraph, which explains it. Asserted over a WINDOW of lines
    # rather than one line: the paragraph is hard-wrapped, so the number sits on
    # a continuation line and a per-line check would pass only by accident of
    # the current wrapping.
    step_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.lstrip().startswith("4.") and "`--reason`" in line
        ),
        None,
    )
    assert step_index is not None, (
        "manual.md no longer describes the --reason requirement at step 4"
    )
    window = "\n".join(lines[step_index : step_index + 14])
    assert "256" in window, (
        "the step-4 paragraph must name the bound within its own text, not "
        "only in the flag table"
    )
    assert "code point" in window, (
        "the bound is stated without its unit, so a Cyrillic reason is "
        "misjudged by a reader who assumed bytes - `len()` counts code points"
    )



def test_api_access_states_the_bound_as_a_family_rule() -> None:
    """`--reason` and `--topic-for` share one bound; the doc says so once."""
    api = _tracked_doc("references/API_ACCESS.md")
    assert "256" in api
    assert "code points" in api
    assert "--reason" in api and "--topic-for" in api, (
        "the bound is documented for one flag only, so it reads as that flag's "
        "quirk rather than a rule the resolver enforces everywhere"
    )


def test_data_caveats_records_the_one_select_per_project_limit() -> None:
    """`test.md` §7's resolver limitation, recorded rather than fixed.

    It is a scope limit, not a defect: `validate_resolved_document` requires a
    populated `selection` per project and `selections` is keyed by project code.
    """
    caveats = _tracked_doc("references/DATA_CAVEATS.md")
    assert "per project" in caveats
    assert "--select" in caveats
    assert "spec.json" in caveats, (
        "the workaround (two independent confirmed runs) is the part a reader "
        "actually needs"
    )


def test_the_error_message_names_the_bound_and_the_remedy() -> None:
    """The number is INTERPOLATED, not typed, so it cannot drift from the rule.

    Asserting on the source rather than on a rendered message is what makes the
    second half provable: a literal `256` in the message would pass a test that
    only checked the rendered text, and would then lie the moment
    `MAX_DISPLAY_CODE_POINTS` changed - which is how the bound came to be
    undocumented in the first place.
    """
    source = (SKILL_ROOT / "scripts" / "resolve_articles.py").read_text(encoding="utf-8")
    assert "must be bounded and control-free" in source
    assert "MAX_DISPLAY_CODE_POINTS} code points" in source, (
        "the bound is typed as a literal rather than interpolated"
    )
    # No message may hardcode the number.
    for line in source.splitlines():
        if "code points" in line and "MAX_DISPLAY_CODE_POINTS" not in line:
            assert "256" not in line, f"a bound is hardcoded here: {line.strip()}"


def test_the_bound_is_still_enforced_and_never_silently_truncated() -> None:
    """Documenting the limit must not have relaxed it or truncated the input.

    A truncated justification is a false record of why an article was chosen,
    so over-long input is still refused rather than shortened.
    """
    import resolve_articles as resolver

    assert resolver.MAX_DISPLAY_CODE_POINTS == 256
    with pytest.raises(resolver.ResolveInputError) as caught:
        resolver._required_text("x" * 257, "--reason")
    assert "256" in str(caught.value)

    long_reason = "y" * 400
    with pytest.raises(resolver.ResolveInputError):
        resolver._contract_text(long_reason, "projects[0].selection.reason")
    # Exactly at the bound is still accepted: the limit is inclusive.
    assert resolver._contract_text("z" * 256, "field") == "z" * 256


# --- The owner's workspace ruling of 2026-09-27, and the gate that holds it ----------


def test_result_is_published_not_ignored() -> None:
    """`result/` holds every run's output AND it is published, subfolders included.

    The ruling, in two halves, and the second half is the one that can rot:

      * one folder per request, always under `result/`, never a sibling `out-<name>/` at
        the skill root. That is why the `out-bizproc-uk-en/`, `out-ai-automation/` and
        `out-ai-automation-n8n/` siblings were noise: untracked run output is one
        `git add -A` away from a public repository - which is how `bug.md` got committed
        in Phase 8 - and it was scattered to review.
      * the run folders are PUBLISHED. `report.md`, the PNGs, `series.csv`,
        `metrics.json` and the optional `report.pdf` are the evidence a visitor reads, so
        the folder that holds them is tracked. `out/` remains ignored because it is the
        `--out` scratch default of a bare stage invocation.

    Three assertions, and each one catches a distinct silent failure:
      * there is no `result` rule in `.gitignore` at all - a stale rule from the earlier
        ruling would unpublish everything here, and `.gitignore` would still LOOK right;
      * a run folder is actually TRACKED (`git ls-files`, not the working tree) - a
        `git rm --cached` or a forgotten `git add` leaves the files on disk and the
        README links to a folder a GitHub visitor cannot open;
      * a subfolder of a run folder is tracked too, because "published together with its
        subfolders" is the actual requirement and `report.pdf`/`logscale/` live one level
        below the run folder. A rule of the form `result/*/` would pass a top-level-only
        check.
    """
    import subprocess

    result_dir = SKILL_DIR / "result"
    assert result_dir.is_dir(), (
        f"{result_dir} is gone. The ruling publishes it, so its absence means the folder "
        f"was deleted rather than moved."
    )

    rules = {
        line.strip().rstrip("/")
        for line in _read(SKILL_DIR / ".gitignore").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert "result" not in rules, (
        f".gitignore still excludes result/ (its rules are {sorted(rules)}). The owner's "
        f"revised ruling publishes the run folders - a stale rule here silently keeps "
        f"every report.md, PNG and series.csv out of the repository."
    )

    tracked = subprocess.run(
        ["git", "ls-files", "--", "wikipedia-trend-agent/result"],
        capture_output=True, text=True, cwd=str(SKILL_DIR.parents[1]), check=False,
    )
    if tracked.returncode != 0:
        # No git repository above the skill (a shipped copy, a temp tree). The ruling's
        # ignore-side assertion above already ran; there is nothing further to check.
        return
    files = [line for line in tracked.stdout.splitlines() if line.strip()]
    assert files, (
        "nothing under `result/` is TRACKED. The files are on disk but not published - a "
        "clone of this repository would not contain them and every README link into "
        "`result/` would 404."
    )
    assert any("/result/" in line and line.count("/") >= 3 for line in files), (
        f"only the top level of `result/` is tracked, so the ruling's 'together with its "
        f"subfolders' half is unmet: {files}. `report.pdf` and `logscale/` sit one level "
        f"below a run folder."
    )
