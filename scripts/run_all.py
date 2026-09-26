"""Run the whole pageviews pipeline in one command: fetch -> analyze -> charts -> report.

Every stage receives the SAME ``--spec`` and the SAME ``--out``; the first
non-zero stage stops the run and nothing after it runs. The exit contract is
built so a model with no other context can decide what to do: ``0`` is success,
``2`` is a spec problem the model fixes, and any other code is a crash or a
fatal the model reports verbatim. A ``2`` comes ONLY from the
``SystemExit(2)`` that ``common.load_and_validate_spec`` raises — no stage's
own return value can be ``2``, so the two meanings can never be confused.

Stages are invoked by IMPORTING each stage's ``main()`` and calling it with an
argv list, NOT by spawning one child process per stage. One interpreter means
one matplotlib import instead of four, there is no dependence on an interpreter
path (Windows-safe), the same ``--out`` reaches every stage, and the fetch
stage's keyword-only ``transport`` seam stays injectable so the whole pipeline
is testable with no network at all. A reader who wants to "just shell out to
each stage" must read this paragraph first. ``make_charts`` is called without
its opt-in ``--log-scale``, so this one command always produces the default
linear regime; a caller who wants the log regime runs the chart stage directly.
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from datetime import date
from types import ModuleType
from typing import cast

import analyze_trends
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

_STAGE_MODULES: dict[str, ModuleType] = {
    module_name: sys.modules[module_name] for _, module_name in STAGES
}


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: Transport | None = None,
    today_utc: date | None = None,
) -> int:
    """Sequence the four stages and return an actionable exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, help="path to spec.json")
    parser.add_argument("--out", default="out", help="output directory (default: out)")
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    args = parser.parse_args(argv)
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
            print(f"run_all: stage {stage_name} failed: exited {exit_code}", file=sys.stderr)
            return exit_code
        if code != 0:
            # Passed through unchanged. A `3` from fetch stays a `3`: a CSV
            # missing a series makes the next stage raise anyway, so continuing
            # would only trade a named failure for an unnamed one.
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
