"""Mechanical packaging gate for RUN-02 (SKILL.md, references/, assets/).

WHAT THIS MODULE IS. These are the gate. Every rule the roadmap's SC#3 names
for packaging is here as a named test that fails rather than as a review
comment: the frontmatter activation surface, the agentskills.io line budget,
the progressive-disclosure link graph CLOSED IN BOTH DIRECTIONS (a dangling
link and an orphan file are both failures), the four-file `references/` set,
and the cross-checks that bind the documentation's vocabulary to the code that
emits it.

WHY `references/` IS EXACTLY FOUR FILES - BY DECISION, NOT BY OMISSION.
`research/STACK.md` proposed a `references/` directory of
`REFERENCE.md`, `REPO_SOURCES.md` and `LICENSE_NOTES.md` alongside the contract.
`REFERENCE.md` is deliberately NOT created: `CONTRACTS.md` plus
`INTERPRETATION.md`, `API_ACCESS.md` and `DATA_CAVEATS.md` already cover the
material, and a second copy of a frozen field table is a second thing that can
drift. `REPO_SOURCES.md` and `LICENSE_NOTES.md` are deliberately NOT created
either: sources and licensing are what a human opens `README.md` and `LICENSE`
for, not something a model loads at question time. The ROADMAP's Phase-7
research flags record the same decision at merge time. Do not "fix" this by
creating the missing files - extend an existing one instead.

WHAT THIS MODULE DELIBERATELY DOES NOT CHECK. It does not verify that the prose
is TRUE. It checks that the vocabulary is real (every reason string is an
`analyze_trends` constant, every confidence/trend value is reachable from the
analyzer), that the links resolve in both directions, and that the structure
fits the spec. Whether the interpretation guidance is *good* is a human read
plus 07-03's README drift test, not something a regex can settle.

THE `skills-ref` TEST AT THE BOTTOM IS OPT-IN CORROBORATION ONLY. It needs
`npx` and the network, so it is skipped unless `WTI_SKILLS_REF=1` is set
explicitly. It is never the gate: if the validator is unavailable, these
mechanical checks are what stands.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

import analyze_trends
import build_report
import run_all

SKILL_DIR = Path(__file__).resolve().parents[1]
SKILL_MD = SKILL_DIR / "SKILL.md"

# The four files, named here as well as in the test that pins them, so the
# decision is legible from the top of the module.
DECIDED_REFERENCE_FILES = frozenset(
    {"CONTRACTS.md", "INTERPRETATION.md", "API_ACCESS.md", "DATA_CAVEATS.md"}
)

# The reason constants INTERPRETATION.md must quote, written out by NAME so a
# rename upstream is a visible edit in this file. Read with getattr, never
# copied as a literal: a constant deleted upstream raises here instead of
# quietly shrinking the required set.
REASON_CONSTANTS = (
    "PERIOD_LONG_REASON",
    "PERIOD_MINIMUM_REASON",
    "PERIOD_BELOW_MINIMUM_REASON",
    "VOLUME_HIGH_REASON",
    "VOLUME_MINIMUM_REASON",
    "VOLUME_BELOW_MINIMUM_REASON",
    "NO_ANOMALIES_REASON",
    "LIMITED_ANOMALIES_REASON",
    "HIGH_ANOMALIES_REASON",
    "MISSING_CLEAN_Y1_REASON",
    "METHODOLOGY_CROSSING_REASON",
    "LOW_CONFIDENCE_HYPOTHESIS_REASON",
    "INSUFFICIENT_OBSERVATIONS_REASON",
    "ZERO_PREVIOUS_MEAN_REASON",
)

# A closed list of plausible-sounding values a documentation file might invent.
# The interesting half of the enum test is that the doc cannot name something
# the analyzer never emits - a doc that says confidence may be "certain" is
# wrong in a way no reachability check on the code would catch.
FORBIDDEN_ENUM_VALUES = (
    "certain",
    "very high",
    "very low",
    "definitive",
    "spiking",
    "surging",
    "exploding",
    "moderate",
)

# Paths SKILL.md may name, and the pattern that finds them.
_LINK_PATTERN = re.compile(r"(?:references|assets)/[A-Za-z0-9._-]+\.(?:md|json)")


def _read(path: Path) -> str:
    """One committed documentation file, read as UTF-8."""
    return path.read_text(encoding="utf-8")


def _frontmatter(text: str) -> dict[str, str]:
    """Parse the `---`-delimited YAML frontmatter WITHOUT a YAML dependency.

    Only the shapes the agentskills.io spec needs are supported: `key: value`
    scalars and one level of nested keys. A description wrapped across several
    lines is joined with single spaces, because the spec's 1024-character limit
    is measured on the value, not on the source lines. Anything the reader does
    not recognise is a raised error, not a silent omission - a frontmatter that
    half-parsed would make every other test in this file quietly vacuous.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise AssertionError("SKILL.md does not open with a '---' frontmatter fence")
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration:
        raise AssertionError("SKILL.md frontmatter is never closed with a '---'") from None

    fields: dict[str, str] = {}
    pending_key: str | None = None
    for line in lines[1:end]:
        if not line.strip():
            continue
        indented = line[:1].isspace()
        stripped = line.strip()
        if indented and pending_key is not None:
            fields[pending_key] = f"{fields[pending_key]} {stripped}".strip()
            continue
        if ":" not in stripped:
            raise AssertionError(f"unrecognised frontmatter line: {line!r}")
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        fields[key] = value
        pending_key = key
    return fields


def _body(text: str) -> str:
    """Everything after the closing `---` of the frontmatter."""
    lines = text.splitlines()
    end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    return "\n".join(lines[end + 1 :])


def _linked_paths() -> set[str]:
    """Every `references/*.md` / `assets/*.json` path SKILL.md's BODY names."""
    return set(_LINK_PATTERN.findall(_body(_read(SKILL_MD))))


def test_frontmatter_name_is_the_directory_name() -> None:
    """`name` must equal the skill directory's own name.

    The agentskills.io spec makes the directory name the activation key, so a
    frontmatter that disagrees with the folder it ships in is a skill nothing
    can load. The positive and the negative are one statement: a name that is
    merely non-empty is not enough.
    """
    frontmatter = _frontmatter(_read(SKILL_MD))
    name = frontmatter.get("name", "")
    assert name != "", "SKILL.md frontmatter has no 'name'"
    assert name == SKILL_DIR.name


def test_description_is_present_and_within_the_spec_limit() -> None:
    """`description` exists, fits 1024 characters, and still carries its triggers.

    The description is what a cheap model reads to decide whether to load this
    skill at all, so shortening it is a behaviour change. Three substrings are
    asserted because the three activation cases are what the skill is selected
    on:

    1. a growing-interest trigger - "is growing";
    2. a cross-language comparison trigger - the brief accepts either the Latin
       "language" or the Cyrillic "мовн" here, so at least one must be present;
    3. a trust/confidence trigger - "can be trusted".

    The not-a-demand-forecast boundary is asserted in the BODY, not here, and
    that is a deliberate correction rather than an omission: the shipped
    `description` has never carried the phrase, and this plan's Task 1 freezes
    the frontmatter byte-for-byte. The boundary IS load-bearing for selection,
    so it is pinned where it actually lives - in `## When to use`, three lines
    below - rather than left unchecked. The description is 584 of 1024
    characters, so an owner who decides the phrase belongs in the activation
    surface has room for it; that is a frontmatter change and a decision, not
    something a test should smuggle in.
    """
    frontmatter = _frontmatter(_read(SKILL_MD))
    description = frontmatter.get("description", "")
    assert description != "", "SKILL.md frontmatter has no 'description'"
    assert len(description) <= 1024, f"description is {len(description)} characters, limit is 1024"

    assert "is growing" in description, "the growing-interest trigger was lost"
    assert ("language" in description) or ("мовн" in description), (
        "the cross-language comparison trigger was lost (neither 'language' nor 'мовн')"
    )
    assert "can be trusted" in description, "the trust/confidence trigger was lost"

    # Markdown hard-wraps, so the boundary phrase is compared against a
    # whitespace-collapsed copy: matching raw text would make the assertion a
    # test of where the author happened to break the line.
    body = re.sub(r"\s+", " ", _body(_read(SKILL_MD)))
    assert "article interest ≠ product demand" in body, (
        "the not-a-demand-forecast boundary was lost from '## When to use'"
    )
    assert "willingness to pay" in body or "ability-to-pay" in body, (
        "the demand/ability-to-pay boundary was lost"
    )


def test_body_is_under_five_hundred_lines() -> None:
    """The agentskills.io line budget, with a non-vacuity guard.

    A malformed file could satisfy `len(body) < 500` by yielding an EMPTY body,
    so the guards come first: the frontmatter must actually have been split off
    (two `---` fences, and the helper raises otherwise) and the body must be
    non-empty.
    """
    text = _read(SKILL_MD)
    body = _body(text)
    assert body.strip() != "", "the SKILL.md body is empty, so the line budget proves nothing"
    assert text.splitlines().count("---") >= 2
    lines = body.splitlines()
    assert len(lines) < 500, f"SKILL.md body is {len(lines)} lines, budget is under 500"


def test_every_referenced_path_exists() -> None:
    """Every `references/` and `assets/` path SKILL.md names must exist on disk.

    Progressive disclosure is a promise: the model is told to go and read a
    file, so a dangling path is a dead end mid-task. The collected set is
    asserted NON-EMPTY first, because a SKILL.md that named nothing would
    otherwise pass this test perfectly. Resolution is done with resolve-and-
    compare, so a path escaping the skill directory fails rather than
    accidentally reading something outside it.
    """
    linked = _linked_paths()
    assert linked, "SKILL.md names no references/ or assets/ path at all"
    for relative in sorted(linked):
        candidate = (SKILL_DIR / relative).resolve()
        assert candidate.is_file(), f"SKILL.md names a path that does not exist: {relative}"
        assert SKILL_DIR.resolve() in candidate.parents, (
            f"SKILL.md names a path outside the skill directory: {relative}"
        )


def test_no_orphan_reference_or_asset_file() -> None:
    """The other direction: a file nothing points at is a failure too.

    `test_every_referenced_path_exists` cannot see this. Together the two close
    the graph, which is what makes "load one file per question" real: every
    file is reachable, and nothing is unreachable.
    """
    linked = _linked_paths()
    on_disk = {
        f"{directory.name}/{path.name}"
        for directory in (SKILL_DIR / "references", SKILL_DIR / "assets")
        for path in sorted(directory.iterdir())
        if path.is_file() and path.suffix in {".md", ".json"}
    }
    assert on_disk, "references/ and assets/ are both empty, so this proves nothing"
    orphans = sorted(on_disk - linked)
    assert not orphans, f"nothing in SKILL.md points at: {orphans}"


def test_references_is_exactly_the_four_decided_files() -> None:
    """`references/` is closed at four files, BY DECISION. A fifth file fails.

    `research/STACK.md` proposed `REFERENCE.md`, `REPO_SOURCES.md` and
    `LICENSE_NOTES.md` beside the contract. They are deliberately absent:
    `CONTRACTS.md` plus `INTERPRETATION.md`, `API_ACCESS.md` and
    `DATA_CAVEATS.md` already cover the material, and sources and licensing
    belong in `README.md` and `LICENSE` (written in plan 07-03) rather than in
    something a model loads at question time. The ROADMAP's Phase-7 research
    flags record the same decision at merge time.

    The set is asserted EXACTLY, not as a superset, so adding a fifth file
    without a decision is a failing test rather than a silent fork of the
    contract.
    """
    found = {path.name for path in (SKILL_DIR / "references").glob("*.md") if path.is_file()}
    assert found == set(DECIDED_REFERENCE_FILES), (
        f"references/ drifted from the decided set: extra={sorted(found - DECIDED_REFERENCE_FILES)}, "
        f"missing={sorted(set(DECIDED_REFERENCE_FILES) - found)}"
    )


def test_the_stale_v07_note_is_gone_and_the_resolver_pre_stage_survives() -> None:
    """The stale bullet is gone AND the Phase-4 pre-stage survived the rewrite.

    One claim, three absences and three presences, because the rewrite had two
    opposite failure modes: leaving the model with a promise the code does not
    keep, and shortening the file by deleting the human-confirmed resolver
    pre-stage. The resolver phrases are load-bearing, not stylistic:
    `status="confirmed"` is the gate that stops a model authoring a spec from
    an unconfirmed article, and `--select PROJECT=ARTICLE` is the only form the
    offline confirmation run accepts.
    """
    text = _read(SKILL_MD)
    assert "v0.7" not in text, "the stale 'arrives in v0.7' note is back"
    assert "arrives in" not in text, "a stale 'arrives in' note is back"
    assert "run_all.py" in text, "SKILL.md no longer names the one command"
    assert 'status="confirmed"' in text, "the resolver confirmation gate was lost"
    assert "--select PROJECT=ARTICLE" in text, "the --select form was lost"
    assert "offline and" in text and "candidate-bounded" in text, (
        "the 'second run is offline and candidate-bounded' sentence was lost"
    )


def test_interpretation_quotes_only_real_analyzer_reasons() -> None:
    """Every reason string INTERPRETATION.md quotes is one the code can emit.

    Three checks. (i) Every reason constant named in `REASON_CONSTANTS` appears
    verbatim in the doc. (ii) Every reason key `build_report.REASON_TOKENS`
    maps for `uk` appears too - the same set a rendered report is written from,
    so the doc and the document cannot disagree about what a reason means.
    (iii) A non-empty guard on both sets, so a rename upstream that emptied
    `REASON_CONSTANTS` here, or an emptied token table, cannot make (i) or (ii)
    pass vacuously.
    """
    text = _read(SKILL_DIR / "references" / "INTERPRETATION.md")
    constants = [getattr(analyze_trends, name) for name in REASON_CONSTANTS]
    assert constants, "the reason-constant list resolved to nothing"
    assert len(set(constants)) == len(constants), "two constants share a value"

    missing = [value for value in constants if value not in text]
    assert not missing, f"INTERPRETATION.md does not quote: {missing}"

    reason_keys = list(build_report.REASON_TOKENS["uk"])
    assert reason_keys, "build_report.REASON_TOKENS['uk'] is empty"
    unmapped = [key for key in reason_keys if key not in text]
    assert not unmapped, (
        f"INTERPRETATION.md and a rendered report disagree about these reasons: {unmapped}"
    )


def test_interpretation_names_only_reachable_confidence_and_trend_values() -> None:
    """The doc may name only the confidence/trend values the analyzer can emit.

    The 06-04 measurement, reused: call `score_confidence` and `safe_direction`
    over their documented input grids, collect what they actually return, and
    require the doc to stay inside those sets. Both sets are asserted NON-EMPTY
    with a per-grid call count, so an argument-order mistake cannot empty a set
    and make the containment vacuously true.

    The complement assertion is the interesting half: a closed forbidden list is
    scanned for, so a doc that invented "certain" or "very high" fails here even
    though every real value is present.
    """
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

    text = _read(SKILL_DIR / "references" / "INTERPRETATION.md").casefold()
    for level in levels:
        assert level in text, f"INTERPRETATION.md never names the reachable level {level!r}"
    for direction in directions:
        assert direction in text, (
            f"INTERPRETATION.md never names the reachable direction {direction!r}"
        )
    for invented in FORBIDDEN_ENUM_VALUES:
        assert invented not in text, (
            f"INTERPRETATION.md names {invented!r}, which the analyzer can never emit"
        )


def test_the_skill_documents_exactly_the_flags_run_all_defines() -> None:
    """SKILL.md's flag documentation is cross-checked against the real parser.

    The doc is read from `run_all._parser()` - the module-level factory, the
    same seam `make_charts` and `build_report` expose - so the two cannot drift
    apart. The second half is the one that catches drift after the fact: a long
    option SKILL.md documents that the parser does not define is a
    documentation bug a reader would hit at the command line.
    """
    parser = run_all._parser()
    defined = {
        option
        for action in parser._actions
        for option in action.option_strings
        if option.startswith("--")
    }
    assert {"--spec", "--out", "--verbose", "--pdf"} <= defined, (
        f"run_all's parser no longer defines the documented flags: {sorted(defined)}"
    )

    text = _read(SKILL_MD)
    documented = set(re.findall(r"--[a-z][a-z-]*", text))
    for flag in ("--spec", "--out", "--verbose", "--pdf"):
        assert flag in documented, f"SKILL.md does not document {flag}"

    # `run_all` owns four flags, so anything else SKILL.md mentions must belong to a
    # DIFFERENT CLI (the resolver's, or the chart stage's opt-in --log-scale) - which is
    # checked against the real sibling parsers rather than against a hand-kept list.
    other_stage_flags = {
        option
        for module in (
            build_report, __import__("make_charts"),
            __import__("resolve_articles"), __import__("build_pdf"),
        )
        for action in module._parser()._actions
        for option in action.option_strings
        if option.startswith("--")
    }
    # NOT pipeline flags, and named as such rather than left to be "fixed" by a reader who
    # assumes every `--word` in SKILL.md is one. These belong to the EXTERNAL `winget`
    # command that installs the typst binary, and the document would be wrong without them
    # - a `winget install` line that silently drops `--silent` and the agreement flag
    # prompts, or refuses, on the machine the reader is standing at.
    external_tool_flags = {"--silent", "--accept-package-agreements"}
    known = defined | other_stage_flags | external_tool_flags
    unknown = sorted(documented - known)
    assert not unknown, f"SKILL.md documents flags no CLI defines: {unknown}"


@pytest.mark.slow
def test_skills_ref_validate_when_enabled() -> None:
    """OPT-IN corroboration. Skipped by default; the mechanical checks are the gate.

    The official agentskills.io validator needs `npx` and the network, so it is
    not something the default suite may depend on. It is enabled only by
    `WTI_SKILLS_REF=1`.

    When it IS enabled and `npx` is missing, this test **FAILS** rather than
    skipping: the owner asked for the validation, and silently skipping would
    report a corroboration that never ran - the failure mode WINDOWS #20 exists
    to record.
    """
    if os.environ.get("WTI_SKILLS_REF") != "1":
        pytest.skip(
            "corroboration only; the mechanical checks in this module are the gate "
            "(set WTI_SKILLS_REF=1 to run the official skills-ref validator)"
        )

    executable = shutil.which("npx")
    if executable is None:
        pytest.fail(
            "WTI_SKILLS_REF=1 asked for the skills-ref validation, but npx is not on PATH. "
            "Install Node.js, or unset WTI_SKILLS_REF and rely on the mechanical checks."
        )

    # The RESOLVED path, not the bare name. On Windows `npx` resolves to
    # `npx.CMD` through PATHEXT, and `subprocess` does not apply PATHEXT: a bare
    # `"npx"` dies with FileNotFoundError [WinError 2] on a machine where npx is
    # plainly installed, which is a failure about this test rather than about
    # the skill. `shutil.which` returns the full path, which launches.
    result = subprocess.run(
        [executable, "--yes", "skills-ref", "validate", str(SKILL_DIR)],
        timeout=180,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            f"skills-ref validate exited {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
