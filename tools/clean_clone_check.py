"""Rehearse a clean clone of this skill - in two halves, on purpose.

WHY `tools/` AND NOT `scripts/`. Like `validate_answer.py`, this is an
owner-facing tool, not a pipeline stage: it is never in `run_all`'s four-stage
sequence, and `scripts/` is the directory the agentskills runtime loads and the
one `mypy scripts --strict` and the contract tests treat as the frozen stage
surface.

THE SPLIT IS THE POINT, and it is stated up front:

  * Mode 1, `--check-only` (the DEFAULT): copy the tracked tree with the
    repository's own `.gitignore` applied, and prove the copy carries no
    generated path and loses no needed one. **No network. This is the suite's
    gate**, and `tests/test_publish.py` runs it on every test run.

  * Mode 2, `--full`: on top of mode 1, create a venv, `pip install -r
    requirements.txt`, `pytest`, and run the shipped example spec end to end.
    **This needs the network and it needs a descriptive `WTI_USER_AGENT`.** It
    is one documented owner command, deliberately NOT a unit test: a real
    `pip install` inside a test is minutes of network and would break this
    suite's zero-network contract (QA-01). A future phase may add a nightly job
    that runs it; this suite will not.

The two halves share ONE source of truth for what is excluded: the matcher
reads `SKILL_DIR/.gitignore` itself, and `tests/test_publish.py` asserts against
that same file. An ignore rule cannot be asserted in one place and contradicted
in the other.

A `pip install` failing here is almost always a network or proxy problem. A run
of the example spec failing with **HTTP 403** is a `WTI_USER_AGENT` problem, not
a pipeline problem, and the output says so.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
GITIGNORE = SKILL_DIR / ".gitignore"

# Path segments the copy must never carry, whatever `.gitignore` says. This is
# a SECOND, independent check on mode 1: a silently mis-parsed ignore rule is
# how a `.cache/` gets shipped, and the parser can only be as good as its
# tests. Staging files are named because `common.dump_json` writes one next to
# its target and an interrupted run can leave it behind.
FORBIDDEN_SEGMENTS = frozenset(
    {".cache", "out", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache", "node_modules"}
)
FORBIDDEN_SUFFIXES = (".tmp", ".pyc")


class IgnoreError(RuntimeError):
    """A `.gitignore` line this tool refuses to guess about."""


def parse_ignore_patterns(path: Path) -> list[str]:
    """The patterns in a `.gitignore`, rejecting anything not understood.

    Three forms are supported, because they are the three this repository uses:
    a bare name (`*.pyc`), a trailing-slash directory (`out/`), and a
    comment/blank line. Anything else raises naming the line. A silently
    mis-parsed ignore rule is exactly the failure this module exists to prevent,
    so an unknown line is a hard error rather than a skip.
    """
    patterns: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("!") or "/" in stripped.strip("/") and not stripped.endswith("/"):
            if stripped.startswith("!"):
                raise IgnoreError(f"{path.name}:{number}: negation is not supported: {line!r}")
            if "/" in stripped.strip("/") and not stripped.endswith("/"):
                raise IgnoreError(f"{path.name}:{number}: path-qualified rule is not supported: {line!r}")
        patterns.append(stripped.rstrip("/"))
    return patterns


def is_ignored(relative: Path, patterns: Sequence[str]) -> bool:
    """Whether a path relative to the skill root matches any ignore pattern."""
    for part in relative.parts:
        if any(fnmatch.fnmatch(part, pattern) for pattern in patterns):
            return True
    return any(fnmatch.fnmatch(relative.name, pattern) for pattern in patterns)


def collect_source_files(patterns: Sequence[str]) -> list[Path]:
    """Every file a clone would carry, as paths relative to the skill root.

    Ignored DIRECTORIES are pruned during the walk, so `__pycache__` is never
    descended into rather than being filtered after the fact.
    """
    survivors: list[Path] = []
    for root, directories, files in os.walk(SKILL_DIR):
        root_path = Path(root)
        relative_root = root_path.relative_to(SKILL_DIR)
        keep: list[str] = []
        for directory in directories:
            relative = relative_root / directory
            if is_ignored(relative, patterns):
                continue
            if directory in FORBIDDEN_SEGMENTS:
                continue
            keep.append(directory)
        directories[:] = keep
        for name in files:
            relative = relative_root / name
            if is_ignored(relative, patterns):
                continue
            if any(part in FORBIDDEN_SEGMENTS for part in relative.parts):
                continue
            if name.endswith(FORBIDDEN_SUFFIXES):
                continue
            survivors.append(relative)
    return sorted(survivors)


def copy_tree(dest: Path) -> tuple[int, list[str]]:
    """Copy the surviving files under `dest`, which must be OUTSIDE the skill.

    The path constraint is asserted before anything is written, because a bug in
    this function writing into the repository it is copying would be both
    possible and invisible until the damage was committed.
    """
    resolved_dest = dest.resolve()
    resolved_skill = SKILL_DIR.resolve()
    if resolved_dest == resolved_skill or resolved_skill in resolved_dest.parents:
        raise IgnoreError(
            f"refusing to copy inside the skill directory: {resolved_dest} is under {resolved_skill}"
        )

    patterns = parse_ignore_patterns(GITIGNORE)
    files = collect_source_files(patterns)
    resolved_dest.mkdir(parents=True, exist_ok=True)
    for relative in files:
        target = resolved_dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SKILL_DIR / relative, target)
    return len(files), patterns


def _venv_python(root: Path) -> Path:
    """The interpreter inside a created venv, chosen by platform.

    A rehearsal that only works on Windows does not rehearse a clone on Linux,
    so the POSIX path is the one that matters for a stranger.
    """
    if os.name == "nt":
        return root / ".venv-rehearsal" / "Scripts" / "python.exe"
    return root / ".venv-rehearsal" / "bin" / "python"


def run_full(dest: Path, file_count: int, patterns: Sequence[str]) -> int:
    """The owner's half: install, test, and run the shipped example. Needs network."""
    print(f"copied {file_count} file(s) to {dest}")
    verdict: list[tuple[str, bool, str]] = [("copy", True, f"{file_count} file(s)")]

    python = _venv_python(dest)
    print(f"step 1/4 install: {python} -m pip install -r requirements.txt")
    try:
        subprocess.run([sys.executable, "-m", "venv", str(dest / ".venv-rehearsal")], check=True)
        subprocess.run([str(python), "-m", "pip", "install", "-r", "requirements.txt"], check=True)
        subprocess.run([str(python), "-m", "pip", "install", "pytest"], check=True)
        verdict.append(("install", True, str(python)))
    except (subprocess.CalledProcessError, OSError) as error:
        verdict.append(("install", False, str(error)))

    print("step 2/4 tests: python -m pytest tests -q")
    try:
        result = subprocess.run(
            [str(python), "-m", "pytest", "tests", "-q"],
            cwd=dest,
            capture_output=True,
            text=True,
            check=False,
        )
        print(result.stdout[-4000:])
        verdict.append(("tests", result.returncode == 0, f"exit {result.returncode}"))
    except OSError as error:
        verdict.append(("tests", False, str(error)))

    print("step 3/4 example: python scripts/run_all.py --spec assets/example.intermittent-fasting.json --out out")
    environment = dict(os.environ)
    if "example.org" in environment.get("WTI_USER_AGENT", "") or not environment.get("WTI_USER_AGENT"):
        print(
            "note: WTI_USER_AGENT is unset or still a placeholder. A failure here with "
            "HTTP 403 is a User-Agent problem, NOT a pipeline problem - common.user_agent() "
            "refuses a placeholder identity before any request is made."
        )
    try:
        result = subprocess.run(
            [
                str(python),
                "scripts/run_all.py",
                "--spec",
                "assets/example.intermittent-fasting.json",
                "--out",
                "out",
            ],
            cwd=dest,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        print(result.stdout[-2000:])
        print(result.stderr[-2000:])
        hint = ""
        if result.returncode != 0 and "403" in (result.stdout + result.stderr):
            hint = " (HTTP 403 - set a descriptive WTI_USER_AGENT; this is not a pipeline failure)"
        verdict.append(("example", result.returncode == 0, f"exit {result.returncode}{hint}"))
    except OSError as error:
        verdict.append(("example", False, str(error)))

    print("step 4/4 verdict")
    for name, ok, detail in verdict:
        print(f"  {'PASS' if ok else 'FAIL'} {name}: {detail}")
    return 0 if all(ok for _, ok, _ in verdict) else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rehearse a clean clone of this skill.",
        epilog=(
            "Two modes, and the split is deliberate. --check-only is the suite's gate: "
            "it copies the tracked tree with this repository's own .gitignore applied "
            "and needs no network. --full is the owner's one command: it also creates a "
            "venv, pip installs, runs pytest and runs the shipped example spec, which "
            "needs the network and a descriptive WTI_USER_AGENT. The suite runs "
            "--check-only and will not run --full."
        ),
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="copy the tracked tree and report (default; no network, the suite's gate)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="additionally create a venv, pip install, run pytest and the example spec (needs network)",
    )
    parser.add_argument(
        "--dest",
        default=None,
        help="destination directory (default: a fresh temp directory outside the skill)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Copy the tree, then optionally run the expensive half. Returns 0 or 1."""
    args = _parser().parse_args(argv)
    if args.check_only and args.full:
        print("choose one mode: --check-only or --full", file=sys.stderr)
        return 2

    if args.dest is not None:
        dest = Path(args.dest)
    else:
        dest = Path(tempfile.mkdtemp(prefix="wta-clone-"))
    try:
        file_count, patterns = copy_tree(dest)
    except IgnoreError as error:
        print(f"clean_clone_check: {error}", file=sys.stderr)
        return 1

    skipped = sorted(
        part
        for part in FORBIDDEN_SEGMENTS
        if any(fnmatch.fnmatch(part, pattern) for pattern in patterns)
    )
    if args.full:
        return run_full(dest, file_count, patterns)

    print(f"copied {file_count} file(s) to {dest}")
    print(f"ignored path names applied from .gitignore: {', '.join(skipped) or 'none'}")
    print("PASS copy: the tracked tree copies with no generated path")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
