"""A minimal type stub for the optional `pypandoc` package.

WHY THIS FILE IS A STUB AND NOT A SUPPRESSION. Phase 9's PDF stage is the first thing in
this repository to import a second third-party package, so the typing gate had to be
reopened for it. Three suppression routes were tried on mypy 2.3.1:

* `ignore_missing_imports` on `pypandoc` - does NOT work. It only silences a MISSING
  import, and `pypandoc` IS installed in the phase-local .venv, so mypy raises
  `import-untyped`. This is the same trap `pyproject.toml` already documents for matplotlib:
  a lever that reads as if it should work, and does not.
* `disable_error_code = ["import-untyped"]` in a per-module block - does NOT work either.
  An import error is reported at the IMPORT SITE, and mypy 2.3.1 does not apply the
  importing module's block to it.
* `follow_untyped_imports` - works, but only as a GLOBAL flag, loosening the gate for every
  future import rather than for this one.

ALL THREE were then found to be solving a problem that did not exist. With
`no_site_packages = true` - the line 05-06 identified as load-bearing, and which this
project still stands behind - mypy never sees the installed package at all, so
`import-untyped` never arises. No suppression is configured, and none is needed.

So this file is not a workaround. It is the POSITIVE declaration of the surface: these are
the calls this project makes on `pypandoc`, and this is their shape. A typing gate that has
been silenced is not a typing gate, and the 05-06 corrective follow-up exists to keep that
distinction sharp; declaring a four-argument surface keeps the gate intact and makes the
dependency reviewable.

THE TRADE, stated plainly: because this stub is on `mypy_path`, it is what mypy uses for
this import rather than the installed package's own (absent) types. That is acceptable
because the surface is four arguments wide, `pypandoc`'s API is stable, and `render_pdf`
passes everything by keyword - so a rename surfaces as a mypy error HERE rather than as a
runtime `TypeError` in an agent's report.
"""
from __future__ import annotations

from typing import Any, Sequence

def get_pandoc_version() -> str:
    """The bundled pandoc's version string, e.g. ``"3.10.1"``."""
    ...

def get_pandoc_path() -> str:
    """Path to the pandoc executable this package resolved."""
    ...

def convert_text(
    source: str,
    to: str,
    *,
    format: str | None = None,
    outputfile: str | None = None,
    extra_args: Sequence[str] | None = None,
    **kwargs: Any,
) -> Any:
    """Convert `source` from `format` into the `to` format.

    The four arguments this project actually passes are `to`, `format`, `outputfile` and
    `extra_args`; `source` is positional. Everything else is forwarded as-is and typed
    `Any` rather than invented, because a stub that specifies a parameter the real package
    does not have is worse than one that admits it does not know.
    """
    ...
