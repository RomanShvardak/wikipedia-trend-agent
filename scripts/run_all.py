"""Run the whole pageviews pipeline in one command: fetch -> analyze -> charts -> report.

Every stage receives the SAME ``--spec`` and the SAME ``--out``; the first
non-zero stage stops the run and nothing after it runs. The exit contract is
built so a model with no other context can decide what to do: ``0`` is success,
``2`` is a spec problem the model fixes, and any other code is a crash or a
fatal the model reports verbatim. A ``2`` comes ONLY from the
``SystemExit(2)`` that ``common.load_and_validate_spec`` raises — no stage's
own return value can be ``2``, so the two meanings can never be confused.

``--pdf`` adds ONE further stage, ``build_pdf``, and it is deliberately NOT in
``STAGES``. Three reasons, all of them about not breaking what is frozen:

* ``STAGES`` is the pipeline's order, and ``tests/test_run_all.py`` asserts
  its exact contents. Appending to it would edit a frozen contract to carry an
  opt-in feature.
* ``--pdf`` is a SWITCH ON THE CONVEYER, not a setting of the stage. The
  PDF stage's own settings are ``--out`` and nothing else — the same split that
  keeps ``--ttl-hours`` on ``fetch_pageviews`` and ``--log-scale`` on
  ``make_charts`` off this parser. A caller who wants the log regime runs the
  chart stage directly; a caller who wants a PDF asks the pipeline for one.
* Without the flag, **nothing changes**: no new file, no new line on stdout, the
  same exit code. ``OPTIONAL_STAGES`` is a separate registry precisely so that
  the default path cannot drift.

The PDF stage is LAST, after ``report``, because it reads
``report.manifest.json`` — which ``report`` writes — and appends to its
``formats[]``. If ``report`` fails, ``pdf`` never runs, under the same
first-non-zero rule as every other stage.
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from datetime import date
from types import ModuleType
from typing import cast

import analyze_trends
import build_pdf
import build_report
import common
import fetch_pageviews
import make_charts
from common import Transport, setup_logging

log = common.log

# (stage name used in messages, module name). The single source of truth for
# the order and the names; _STAGE_MODULES is derived from it.
STAGES: tuple[tuple[str, str], ...] = (
    ("fetch", "fetch_pageviews"),
    ("analyze", "analyze_trends"),
    ("charts", "make_charts"),
    ("report", "build_report"),
)

#: (stage name used in messages, module name, the run_all flag that enables it).
#: Run AFTER `STAGES`, and only when its flag is present. Deliberately a SEPARATE registry
#: rather than a fifth entry in `STAGES`: the existing test asserts `STAGES` is exactly
#: `["fetch", "analyze", "charts", "report"]`, and an opt-in feature has no business
#: editing a frozen pipeline's order to carry itself.
OPTIONAL_STAGES: tuple[tuple[str, str, str], ...] = (
    ("pdf", "build_pdf", "--pdf"),
)

_STAGE_MODULES: dict[str, ModuleType] = {
    module_name: sys.modules[module_name] for _, module_name in STAGES
}

_OPTIONAL_STAGE_MODULES: dict[str, ModuleType] = {
    module_name: sys.modules[module_name] for _, module_name, _flag in OPTIONAL_STAGES
}


def _parser() -> argparse.ArgumentParser:
    """Three shared flags plus `--pdf`, which switches on a whole stage.

    A module-level factory, like `make_charts._parser` and `build_report._parser`, so the
    packaging test can read the real option set out of the parser instead of re-deriving it
    from a docstring.

    `--pdf` is the ONE flag here that is not forwarded to the stages: it is consumed by the
    loop below, which decides whether to run `build_pdf` at all. `--ttl-hours` and
    `--log-scale` are the mirror image — they belong to the stage CLIs that own them and are
    called directly. The distinction is ownership, and it is why this parser has four flags
    and the stage parsers have three each.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, help="path to spec.json")
    parser.add_argument("--out", default="out", help="output directory (default: out)")
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    parser.add_argument(
        "--pdf",
        action="store_true",
        help="additionally render report.pdf after the four core stages (opt-in; needs "
             "`pip install -r requirements-pdf.txt` and the typst binary)",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: Transport | None = None,
    today_utc: date | None = None,
) -> int:
    """Sequence the four stages and return an actionable exit code."""
    args = _parser().parse_args(argv)
    setup_logging(args.verbose)

    # No mkdir and no pre-validation here: `fetch_pageviews` is the first stage,
    # it calls `load_and_validate_spec` itself, and a duplicate validation would
    # be a second place that owns the spec problem.
    #
    # A successful chart stage is deliberately NOT a report-stage precondition
    # (CONTRACTS.md §8.3's named Phase 7 constraint): the chart stage accepts
    # `ja` and the report stage does not, so a `ja` spec publishes a valid
    # chart manifest and is then refused at the report stage with exit 1.
    for stage_name, module_name in STAGES:
        stage_main = cast(Callable[..., int], getattr(_STAGE_MODULES[module_name], "main"))
        stage_argv = ["--spec", args.spec, "--out", args.out]
        if args.verbose:
            stage_argv.append("--verbose")
        try:
            if stage_name == "fetch":
                # The ONLY stage called with keywords: the other three take argv
                # alone, and forwarding the transport to them is a TypeError.
                code = stage_main(stage_argv, transport=transport, today_utc=today_utc)
            else:
                code = stage_main(stage_argv)
        except SystemExit as exc:
            # The spec-problem channel, plus any future SystemExit (e.g. the
            # `user_agent()` fail-fast, whose string code is not an int).
            exit_code = exc.code if isinstance(exc.code, int) else 1
            if exit_code == 2:
                print(
                    f"run_all: stage {stage_name} failed: spec problem "
                    "— fix the spec and re-run",
                    file=sys.stderr,
                )
                return 2
            if isinstance(exc.code, str) and exc.code.strip():
                # A SystemExit carrying a MESSAGE is the stage handing the
                # model an action ("set WTI_USER_AGENT to ..."). Python would
                # have printed it had the exception reached the top level;
                # catching it here would silently drop the only actionable
                # line, which is the same defect as swallowing the fetch
                # stage's 403 diagnostic.
                print(exc.code, file=sys.stderr)
            print(f"run_all: stage {stage_name} failed: exited {exit_code}", file=sys.stderr)
            return exit_code
        if code != 0:
            # Passed through unchanged. A `3` from fetch stays a `3`: a CSV
            # missing a series makes the next stage raise anyway, so continuing
            # would only trade a named failure for an unnamed one.
            print(f"run_all: stage {stage_name} failed: exit {code}", file=sys.stderr)
            return code

    # The optional stages, and ONLY those whose flag was given. Same argv, same ordering
    # discipline, same "first non-zero stops the run" rule as the core loop above — so a
    # failing `report` means `pdf` is never reached, which is the whole reason the PDF
    # stage is last rather than first.
    enabled = {
        flag: (name, module_name)
        for name, module_name, flag in OPTIONAL_STAGES
    }
    for flag, (stage_name, module_name) in enabled.items():
        if not getattr(args, flag.lstrip("-").replace("-", "_"), False):
            continue
        stage_main = cast(
            Callable[..., int], getattr(_OPTIONAL_STAGE_MODULES[module_name], "main")
        )
        stage_argv = ["--spec", args.spec, "--out", args.out]
        if args.verbose:
            stage_argv.append("--verbose")
        try:
            code = stage_main(stage_argv)
        except SystemExit as exc:
            exit_code = exc.code if isinstance(exc.code, int) else 1
            if exit_code == 2:
                print(
                    f"run_all: stage {stage_name} failed: spec problem "
                    "— fix the spec and re-run",
                    file=sys.stderr,
                )
                return 2
            if isinstance(exc.code, str) and exc.code.strip():
                print(exc.code, file=sys.stderr)
            print(f"run_all: stage {stage_name} failed: exited {exit_code}", file=sys.stderr)
            return exit_code
        if code != 0:
            # A PDF failure is a REAL failure and is passed through unchanged, exactly as
            # any other stage's is. The tempting alternative — warn and exit 0, because the
            # Markdown report is already written — would make `--pdf` a flag whose failure
            # is invisible, and a caller who asked for a document would be told the
            # pipeline succeeded while holding no document.
            print(f"run_all: stage {stage_name} failed: exit {code}", file=sys.stderr)
            return code

    # No blanket handler anywhere in this module on purpose: a traceback
    # escaping a stage is a crash the model must report verbatim, and a
    # catch-all would convert it into a tidy line that hides the bug. This
    # mirrors the rule `build_report.main` already documents for its own
    # uncaught `SystemExit(2)`.
    print(f"run_all: pipeline complete; output directory: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
