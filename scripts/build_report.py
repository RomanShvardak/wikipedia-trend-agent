"""Render local metrics.json + charts.json + spec.json into a Markdown report and its manifest.

The report stage is a **reader**, never a calculator (ANAL-06, the same rule
CONTRACTS.md §7.3 froze for the chart stage and §7.3's own sentence closes for
every consumer: *a consumer inherits this rule*). It opens `series.csv` at no
point — every number it displays was already published by an earlier stage, so
there is nothing here for it to derive. `series.csv` is the chart stage's input;
reaching for it would be a second, independent read of the raw data and the
first step on the road to a number that disagrees with the manifest.

The module is split in two halves on purpose, in `make_charts`' own shape. The
planning half is pure: `render_report` reads the three documents and returns the
Markdown text plus the ordered `metrics_shown` pointer list, performing no I/O,
so a test can read a number instead of scraping a rendered page. The writing
half is dumb: `dump_text` and `_manifest` map those two values onto the
filesystem and derive nothing.

Exit codes - the frozen CONTRACTS.md §8.7 table, in full:

    0   every section rendered and report.md + report.manifest.json published
    2   spec problem, inherited verbatim from common.load_and_validate_spec's
        SystemExit(2) and never intercepted here
    1   any local input or publication failure

There is deliberately NO `3` (partial) case, for §7.5's reason applied to this
stage: a half-written report is the same defect as a half-populated
charts.json. A reader who finds four sections and a manifest naming six has
been told something false about the other two, and has no way to tell which. So
a document that cannot be completed fails the whole stage: `report.md` lands
only after EVERY section has been assembled, and a failure at any point leaves
the prior bytes untouched with no staging file behind. Two outcomes, never three.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import analyze_trends
import common
import make_charts
from common import dump_json, load_and_validate_spec, setup_logging

log = common.log

# Ratified as the CONTRACTS.md §8 `report.v1` surface by plan 06-01's Task 1
# checkpoint:decision gate — D-18's successor. The "provisional" status is what
# that gate existed to discharge. Phase 7's `run_all.py` and Phase 8's eval both
# code against these ten names, which is why the freeze is a ratification with a
# named surface rather than an implementation detail.
REPORT_CONTRACT_VERSION = "report.v1"
# Bare relative names, no directory component. CONTRACTS.md §8.5's join relies on
# it, and it is what lets a v1.x PDF be one appended `formats[]` object rather
# than a rename of the Markdown slot.
REPORT_FILENAME = "report.md"
REPORT_MANIFEST_FILENAME = "report.manifest.json"

# D-16's rule, one stage downstream: this table is the ONLY source of the words
# the report writes, and a language with no table is REFUSED rather than served
# English. A Ukrainian report with one English heading is a bilingual artefact
# the reader did not ask for, and no test of the arithmetic would catch it — the
# numbers would all be right. This is the same argument charts.v1 §7.4 carries
# for chart text, and `report_tokens` is the same fail-closed lookup.
#
# Every entry is short on purpose: these strings live in a table cell, a
# bullet or a section heading on a one-page document, and a wrapped table header
# is a legibility defect.
REPORT_TOKENS: dict[str, dict[str, str]] = {
    "en": {
        "title": "Wikipedia pageviews analysis",
        "conclusion": "Conclusion",
        "metrics": "Metrics",
        "trust": "How much to trust it",
        "charts": "Charts",
        "limitations": "Limitations and assumptions",
        "next_step": "Next step",
        "as_of": "Data as of",
        "series": "Topic",
        "project": "Project",
        "article": "Article",
        "language": "Language",
        "total_views": "Total views",
        "avg_daily_views": "Views/day",
        "period": "Period",
        "growth": "Growth",
        "window": "window",
        "direction": "Direction",
        "confidence": "Confidence",
        "anomaly_share": "Anomaly share",
        "anomalies": "anomalies",
        "not_computable": "n/a",
        "volume_base": "on base",
        "hypothesis": "hypothesis",
        "source": "Source",
        "measured": "Measured",
        "not_a_forecast": "This is not a forecast and not a recommendation",
        "chart_image_alt": "Chart",
        "assumption": "author assumption",
        # The eleven enum and disclosure words. CONTRACTS.md §8.3 says two
        # tables carry every word the document writes, and until now these eleven
        # were written by neither. A confidence level and a disclosure key are
        # words the reader reads, not numbers, so §8.4 does not reach them and
        # the completeness sweep could not see them. The `en` values are the
        # ASCII words this stage used to write out of the source document, so an
        # English reader's document is unchanged in vocabulary; none of them is a
        # `test_contracts.ROLLUP_KEY_BLACKLIST` word, and the `en` table is not
        # rendered by any test (the committed spec's `language` is `uk`), so a
        # rollup word introduced here would be invisible to the rollup test.
        "confidence_low": "low",
        "confidence_medium": "medium",
        "confidence_high": "high",
        "trend_up": "up",
        "trend_down": "down",
        "trend_flat": "flat",
        "trend_noise": "noise",
        "trend_inconclusive": "inconclusive",
        "disclosure_gaps": "gaps",
        "disclosure_anomalies_drawn": "anomalies_drawn",
        "disclosure_log_masked_points": "log_masked_points",
    },
    "uk": {
        "title": "Аналіз переглядів Wikipedia",
        "conclusion": "Висновок",
        "metrics": "Метрики",
        "trust": "Наскільки можна довіряти",
        "charts": "Графіки",
        "limitations": "Обмеження та припущення",
        "next_step": "Наступний крок",
        "as_of": "Дані станом на",
        "series": "Тема",
        "project": "Проєкт",
        "article": "Стаття",
        "language": "Мова",
        "total_views": "Усього переглядів",
        "avg_daily_views": "Переглядів/день",
        "period": "Період",
        "growth": "Зростання",
        "window": "вікно",
        "direction": "Напрям",
        "confidence": "Впевненість",
        "anomaly_share": "Частка аномалій",
        "anomalies": "аномалій",
        "not_computable": "н/д",
        "volume_base": "на основі",
        "hypothesis": "гіпотеза",
        "source": "Джерело",
        "measured": "Виміряно",
        "not_a_forecast": "Це не прогноз і не рекомендація",
        "chart_image_alt": "Графік",
        # NOT a bare "припущення": that word is inside the «Обмеження та
        # припущення» heading, and the empty-assumptions edge is proven by
        # asserting this token appears NOWHERE in the document — a token that
        # is a substring of a section heading could never be absent.
        "assumption": "припущення автора",
        # The same eleven words, localized. `зростання` is a trend DIRECTION here
        # and is deliberately lowercase so it cannot be confused with the
        # `growth` COLUMN header `Зростання`; the two are different words of the
        # document, and only the column header is a heading.
        "confidence_low": "низька",
        "confidence_medium": "середня",
        "confidence_high": "висока",
        "trend_up": "зростання",
        "trend_down": "спад",
        "trend_flat": "плоске",
        "trend_noise": "шум",
        "trend_inconclusive": "невизначено",
        "disclosure_gaps": "прогалин",
        "disclosure_anomalies_drawn": "намальованих аномалій",
        "disclosure_log_masked_points": "знецінених точок",
    },
}
# The keys a language table MUST define. A partial table is the same defect as a
# missing one — a KeyError raised halfway through a render would leave a
# half-written document on disk — so `report_tokens` checks the whole set and
# reports the language either way.
REQUIRED_REPORT_TOKENS: tuple[str, ...] = (
    "title",
    "conclusion",
    "metrics",
    "trust",
    "charts",
    "limitations",
    "next_step",
    "as_of",
    "series",
    "project",
    "article",
    "language",
    "total_views",
    "avg_daily_views",
    "period",
    "growth",
    "window",
    "direction",
    "confidence",
    "anomaly_share",
    "anomalies",
    "not_computable",
    "volume_base",
    "hypothesis",
    "source",
    "measured",
    "not_a_forecast",
    "chart_image_alt",
    "assumption",
    "confidence_low",
    "confidence_medium",
    "confidence_high",
    "trend_up",
    "trend_down",
    "trend_flat",
    "trend_noise",
    "trend_inconclusive",
    "disclosure_gaps",
    "disclosure_anomalies_drawn",
    "disclosure_log_masked_points",
)
# CONTRACTS.md §8.2's six headings, in the frozen order, named by the token that
# supplies each. The order is a CONTRACT, so it lives in one tuple that the
# renderer walks and a test reads: a renderer that emitted sections in a
# different order and a test that asserted a different order would agree with
# each other and both be wrong.
SECTION_TOKEN_KEYS: tuple[str, ...] = (
    "conclusion",
    "metrics",
    "trust",
    "charts",
    "limitations",
    "next_step",
)

# D-16 for the rubric phrases. `confidence_reasons[]` and `seasonality.note` are
# ENGLISH CONSTANTS in analyze_trends.py, and printing one verbatim inside a
# Ukrainian report is the mixed-language defect T-5-24 named for charts. So every
# such constant is mapped here, keyed by its VERBATIM English value.
#
# The keys are imported BY REFERENCE. That is the whole point: if a constant is
# renamed upstream, this module's import raises at collection time and the
# completeness test fails, instead of the report quietly printing an unmapped
# English string that no test of the arithmetic would catch.
REASON_TOKENS: dict[str, dict[str, str]] = {
    "en": {
        analyze_trends.INSUFFICIENT_OBSERVATIONS_REASON: (
            "insufficient observations in one or both equal-length windows"
        ),
        analyze_trends.ZERO_PREVIOUS_MEAN_REASON: (
            "previous equal-length window has zero mean"
        ),
        analyze_trends.PERIOD_BELOW_MINIMUM_REASON: "period below 91 days",
        analyze_trends.PERIOD_MINIMUM_REASON: "period at least 91 days",
        analyze_trends.PERIOD_LONG_REASON: "period at least 730 days",
        analyze_trends.VOLUME_BELOW_MINIMUM_REASON: "monthly 30-day views below 1000",
        analyze_trends.VOLUME_MINIMUM_REASON: "monthly 30-day views at least 1000",
        analyze_trends.VOLUME_HIGH_REASON: "monthly 30-day views at least 10000",
        analyze_trends.NO_ANOMALIES_REASON: "no anomalies detected",
        analyze_trends.LIMITED_ANOMALIES_REASON: "anomaly share within 5 percent",
        analyze_trends.HIGH_ANOMALIES_REASON: "anomaly share above 5 percent",
        analyze_trends.MISSING_CLEAN_Y1_REASON: "clean 1-year growth unavailable",
        analyze_trends.METHODOLOGY_CROSSING_REASON: (
            "comparison crosses 2015-05-01 methodology break"
        ),
        analyze_trends.LOW_CONFIDENCE_HYPOTHESIS_REASON: (
            "low confidence: treat the reading as a hypothesis"
        ),
        analyze_trends.SEASONALITY_UNAVAILABLE_NOTE: (
            "fewer than two aligned 365-day halves are available"
        ),
        analyze_trends.SEASONALITY_NO_PEAKS_NOTE: "no recurring peak months identified",
        analyze_trends.SEASONALITY_AVAILABLE_NOTE: (
            "recurring peaks have positive shared month effects whose post-subtraction "
            "residual is non-negative in both aligned halves, so a month effect measured "
            "in only one of the two observed years is not reported as recurring; "
            "month-scale monotonic curvature outside the fitted linear/quadratic daily "
            "trend can still be confounded with calendar effects across only two "
            "observed cycles"
        ),
    },
    "uk": {
        analyze_trends.INSUFFICIENT_OBSERVATIONS_REASON: (
            "недостатньо спостережень в одному або в обох однакових за довжиною вікнах"
        ),
        analyze_trends.ZERO_PREVIOUS_MEAN_REASON: (
            "попереднє вікно однакової довжини має нульове середнє"
        ),
        analyze_trends.PERIOD_BELOW_MINIMUM_REASON: "період менший за 91 день",
        analyze_trends.PERIOD_MINIMUM_REASON: "період щонайменше 91 день",
        analyze_trends.PERIOD_LONG_REASON: "період щонайменше 730 днів",
        analyze_trends.VOLUME_BELOW_MINIMUM_REASON: "переглядів за 30 днів менше 1000",
        analyze_trends.VOLUME_MINIMUM_REASON: "переглядів за 30 днів щонайменше 1000",
        analyze_trends.VOLUME_HIGH_REASON: "переглядів за 30 днів щонайменше 10000",
        analyze_trends.NO_ANOMALIES_REASON: "аномалій не виявлено",
        analyze_trends.LIMITED_ANOMALIES_REASON: "частка аномалій у межах 5 відсотків",
        analyze_trends.HIGH_ANOMALIES_REASON: "частка аномалій понад 5 відсотків",
        analyze_trends.MISSING_CLEAN_Y1_REASON: "чисте зростання за рік недоступне",
        analyze_trends.METHODOLOGY_CROSSING_REASON: (
            "порівняння перетинає розрив методології 2015-05-01"
        ),
        analyze_trends.LOW_CONFIDENCE_HYPOTHESIS_REASON: (
            "низька впевненість: трактуйте значення як гіпотезу"
        ),
        analyze_trends.SEASONALITY_UNAVAILABLE_NOTE: (
            "доступно менше двох зіставлених піврічок по 365 днів"
        ),
        analyze_trends.SEASONALITY_NO_PEAKS_NOTE: "повторюваних пікових місяців не виявлено",
        analyze_trends.SEASONALITY_AVAILABLE_NOTE: (
            "повторювані піки мають додатні спільні місячні ефекти, чий залишок після "
            "віднімання є невід'ємним в обох зіставлених півріроках, тому місячний "
            "ефект, виміряний лише в одному з двох спостережених років, не звітується "
            "як повторюваний; позамісячна монотонна кривизна поза лінійним чи "
            "квадратичним денним трендом усе ще може бути сплутана з календарними "
            "ефектами за лише два спостережені цикли"
        ),
    },
}

# D-16 for the two ENUM families a metrics row carries. `REASON_TOKENS` above
# keys its reasons by importing the upstream constant BY REFERENCE, so a rename
# upstream breaks this module at collection time. These two cannot: the values are
# INLINE string literals in analyze_trends.py (`"high" if score >= 4 else ...` at
# :729, `"up"` / `"down"` / `"flat"` at :737/:739/:740 and `"inconclusive"` /
# `"noise"` at :752/:754), so there is nothing to import - which is exactly why
# the CR-01 leak survived: no upstream rename could ever break a key set.
#
# So the maps are keyed on the VERBATIM `metrics.json` VALUE, and completeness is
# proved by MEASURING the reachable set out of `analyze_trends.score_confidence`
# and `analyze_trends.safe_direction` themselves
# (`test_report.test_enum_token_maps_cover_every_value_analyze_trends_can_emit`).
# That test is load-bearing, not decorative: a value added upstream without a
# token fails there instead of printing English inside a Ukrainian document.
CONFIDENCE_TOKEN_KEYS: dict[str, str] = {
    "low": "confidence_low",
    "medium": "confidence_medium",
    "high": "confidence_high",
}
TREND_TOKEN_KEYS: dict[str, str] = {
    "up": "trend_up",
    "down": "trend_down",
    "flat": "trend_flat",
    "noise": "trend_noise",
    "inconclusive": "trend_inconclusive",
}

# GFM table-cell safety. A raw `|` in a spec-authored label splits one cell into
# two and shifts every column after it; a newline inside a cell ends the row
# early and can inject a row the report never wrote. Both are reachable from
# `spec.json`, which is model-authored, so this is a data-path guard and not a
# theoretical one. The substitution happens at the ONE seam every cell value
# passes through (`md_cell`), so a new call site cannot forget it.
_CELL_UNSAFE = re.compile(r"[|\r\n]+")
_CELL_REPLACEMENT = "\\| "


class ReportError(RuntimeError):
    """A model-readable local input or report failure."""


def report_tokens(language: str) -> Mapping[str, str]:
    """D-16: the report's own words for `language`, or a refusal — never English.

    Two states are the same defect and get the same message: a language with no
    table at all, and a table missing one of `REQUIRED_REPORT_TOKENS`. A
    `KeyError` raised mid-render would instead leave a half-written document on
    disk and print a traceback instead of a model-readable line.

    CONTRACTS.md §8.3 is why this raises rather than falling back, and why the
    accepted set is an INTERSECTION with `make_charts.chart_tokens` rather than a
    union: the chart stage accepts `ja`, this table does not, so a `ja` spec
    yields a valid `charts.json` and is then refused HERE with exit 1 and
    nothing written. Phase 7's `run_all.py` must not treat a successful chart
    stage as a report-stage precondition.
    """
    table = REPORT_TOKENS.get(language)
    if table is None or any(key not in table for key in REQUIRED_REPORT_TOKENS):
        raise ReportError(f"no report tokens for language: {language}")
    return table


def reason_token(language: str, reason: str) -> str:
    """The localized phrase for one verbatim English reason, or a refusal.

    Same shape and same reason as `report_tokens`: an unmapped reason is a
    missing translation, and a missing translation inside a Ukrainian report is
    a bilingual artefact. The refusal names the reason verbatim so the caller
    knows which upstream constant to add — which is also what makes an upstream
    RENAME loud instead of silent.
    """
    table = REASON_TOKENS.get(language)
    if table is None or reason not in table:
        raise ReportError(f"no localized phrase for reason: {reason!r}")
    return table[reason]


def _enum_token(language: str, mapping: Mapping[str, str], value: str, kind: str) -> str:
    """The localized phrase for one enum value, or a refusal — never the English word.

    One seam for every enum family, modelled line-for-line on `reason_token`, so a
    new family gets a fail-closed lookup rather than a third spelling of the same
    refusal. Fail-closed on BOTH sides:

    - **No language table, or a table missing the named key.** `report_tokens`
      raises first, and it refuses a PARTIAL table exactly as it refuses a wholly
      missing one, so a table that never grew the eleven new keys cannot render a
      single character.
    - **The value is not in `mapping`.** Raised, with the value named verbatim, so
      the caller knows which upstream value to add. Falling back to the English
      word here would put `low` or `up` into a Ukrainian document — a bilingual
      artefact no test of the arithmetic would catch, because every number would
      still be right.
    """
    table = report_tokens(language)
    key = mapping.get(value)
    if key is None or key not in table:
        raise ReportError(f"no localized phrase for {kind}: {value!r}")
    return table[key]


def confidence_token(language: str, value: str) -> str:
    """The localized phrase for one verbatim `metrics.json` `confidence` level."""
    return _enum_token(language, CONFIDENCE_TOKEN_KEYS, value, "confidence")


def trend_token(language: str, value: str) -> str:
    """The localized phrase for one verbatim `metrics.json` `trend_direction`."""
    return _enum_token(language, TREND_TOKEN_KEYS, value, "trend_direction")


def md_cell(value: object) -> str:
    """One GFM table cell, escaped at the single seam every cell value crosses.

    A `|` becomes `\\|` and a run of newlines becomes a single space, then the
    result is stripped. Applied to EVERY value interpolated into a cell — label,
    project, article, direction, confidence, and every reason or note phrase —
    because a cell escaped at four call sites out of five is a table that renders
    wrong on the fifth.
    """
    text = "" if value is None else str(value)
    return _CELL_UNSAFE.sub(_CELL_REPLACEMENT, text).strip()


def format_number(value: int | float, *, percent: bool = False) -> str:
    """The ONE function through which any number becomes a string in this module.

    Two facts make this a seam rather than a convenience. First, it is the single
    point where PEP 378's specs are applied, and those specs are deliberately
    NOT locale-aware (CONTRACTS.md §8.4): the digits a reader sees are the digits
    the contract froze, in every language. This function must therefore never use
    `locale`, and the module must never import it — the AST test in
    `test_report.py` fails the import, not just this call.

    Second, the keyword-only `percent` flag is what lets one seam serve both a
    count and a signed percentage without each call site choosing a format spec,
    and it is what lets a test monkeypatch EXACTLY ONE function to prove the
    fidelity check is not vacuous. Formatting is display, not derivation: the
    value handed in is the value the document read.
    """
    return f"{value:+.1f}" if percent else f"{value:,}"


def _require_str(container: Mapping[str, Any], key: str, where: str) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value:
        raise ReportError(f"{where}.{key} must be a non-empty string")
    return value


def _require_number(container: Mapping[str, Any], key: str, where: str) -> int | float:
    value = container.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReportError(f"{where}.{key} must be a number")
    return value


def load_charts(path: str | Path) -> tuple[dict[str, object], str]:
    """Read, shape-check, and hash the local charts.json in a single pass.

    `make_charts.load_metrics`'s own shape, for the sibling manifest, and the
    same one-pass discipline: the digest is taken from the exact bytes read, so a
    consumer detects staleness by comparing content and never by comparing
    timestamps. A second read could read different bytes than the one that was
    validated, and the digest would then attest to content this report never saw.

    Deliberately does NOT re-derive any of `charts.json`'s per-chart fields and
    does NOT re-check them against `metrics.json`: a consumer reads the sibling
    manifest, and a second opinion about a frozen document is a second chance to
    disagree with it.
    """
    charts_path = Path(path)
    try:
        raw = charts_path.read_bytes()
    except OSError as error:
        raise ReportError(f"could not read charts JSON: {charts_path}") from error
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReportError(f"charts JSON is malformed: {charts_path}") from error
    if not isinstance(document, dict):
        raise ReportError("charts JSON top level must be an object")
    version = document.get("contract_version")
    if version != make_charts.CHARTS_CONTRACT_VERSION:
        raise ReportError(
            f"charts JSON contract_version must be {make_charts.CHARTS_CONTRACT_VERSION!r}"
        )
    _require_str(document, "spec_name", "charts")
    _require_str(document, "as_of", "charts")
    _require_str(document, "language", "charts")
    charts = document.get("charts")
    if not isinstance(charts, list) or not charts:
        raise ReportError("charts.charts must be a non-empty list")
    for index, entry in enumerate(charts):
        where = f"charts.charts[{index}]"
        if not isinstance(entry, dict):
            raise ReportError(f"{where} must be an object")
        _require_str(entry, "filename", where)
    return document, hashlib.sha256(raw).hexdigest()


def require_display_fields(metrics: Mapping[str, Any]) -> None:
    """The report's own required-key guard, layered ON TOP of `load_metrics`.

    `make_charts.load_metrics` requires only `spec_name`, `as_of` and the per-
    series `series_id` / `label` / `language` / `avg_daily_views` / `growth` /
    `anomalies` — exactly what a CHART needs. It does not cover what this
    document renders: a metrics document can satisfy the chart stage and still
    be missing the `project`, `period`, `trend_direction`, `confidence`,
    `confidence_reasons` or `seasonality` the report puts on the page. Reusing
    the chart guard and stopping there would turn a missing display field into a
    `KeyError` traceback halfway through a render, which is the one output a
    model cannot act on.

    Every failure names the exact `where.key`, so exit 1 arrives with a line
    that says which field to fix.
    """
    series_nodes = metrics.get("series")
    if not isinstance(series_nodes, list) or not series_nodes:
        raise ReportError("metrics.series must be a non-empty list")
    for index, node in enumerate(series_nodes):
        where = f"metrics.series[{index}]"
        if not isinstance(node, dict):
            raise ReportError(f"{where} must be an object")
        for key in ("project", "article"):
            _require_str(node, key, where)
        _require_number(node, "total_views", where)
        _require_number(node, "avg_daily_views", where)
        _require_number(node, "anomaly_share", where)
        for key in ("trend_direction", "confidence"):
            _require_str(node, key, where)
        reasons = node.get("confidence_reasons")
        if not isinstance(reasons, list) or not all(
            isinstance(item, str) and item for item in reasons
        ):
            raise ReportError(f"{where}.confidence_reasons must be a list of non-empty strings")
        period = node.get("period")
        if not isinstance(period, dict):
            raise ReportError(f"{where}.period must be an object")
        for key in ("start", "end"):
            _require_str(period, key, f"{where}.period")
        _require_number(period, "days", f"{where}.period")
        seasonality = node.get("seasonality")
        if not isinstance(seasonality, dict):
            raise ReportError(f"{where}.seasonality must be an object")
        if not isinstance(seasonality.get("months"), list):
            raise ReportError(f"{where}.seasonality.months must be a list")
        _require_str(seasonality, "note", f"{where}.seasonality")
        growth = node.get("growth")
        if not isinstance(growth, dict):
            raise ReportError(f"{where}.growth must be an object")
        for window in analyze_trends.GROWTH_WINDOWS:
            window_node = growth.get(window)
            if not isinstance(window_node, dict):
                raise ReportError(f"{where}.growth.{window} must be an object")
            for key in ("start", "end"):
                _require_str(window_node, key, f"{where}.growth.{window}")
            clean = window_node.get("clean")
            if not isinstance(clean, dict):
                raise ReportError(f"{where}.growth.{window}.clean must be an object")
            pct = clean.get("pct")
            if pct is not None and (
                isinstance(pct, bool) or not isinstance(pct, (int, float))
            ):
                raise ReportError(
                    f"{where}.growth.{window}.clean.pct must be a number or null"
                )
            # ANAL-06: a null pct is only meaningful beside its reason. The
            # report PRINTS that reason, so a window that is null without one
            # would put a bare "n/a" on the page with nothing explaining it.
            if pct is None:
                _require_str(clean, "reason", f"{where}.growth.{window}.clean")


def _growth_phrase(
    tokens: Mapping[str, str], language: str, window_node: Mapping[str, Any]
) -> str:
    """One window's displayed percentage: `clean.pct`, or the refusal with its reason.

    D-03: the CLEAN variant is displayed and the raw `pct` never is, for the same
    reason the growth chart plots only the clean variant — the raw value carries
    the spike the clean variant removed, and printing both would make an excluded
    anomaly look like part of the finding.

    A null is the localized not-computable token followed by that window's own
    `clean.reason`: never `0`, never `0.0`, never an em-dash, never an empty
    cell. `0` would read as "no growth" (ANAL-06) and an em-dash would read as an
    absence the report never diagnosed.
    """
    clean = window_node["clean"]
    pct = clean.get("pct")
    if pct is None:
        reason = reason_token(language, str(clean.get("reason")))
        return f"{tokens['not_computable']} ({md_cell(reason)})"
    return f"{format_number(pct, percent=True)}%"


def render_report(
    spec: Mapping[str, Any],
    metrics: Mapping[str, Any],
    charts: Mapping[str, Any],
) -> tuple[str, list[str]]:
    """The pure planning half: the Markdown text and the ordered `metrics_shown` list.

    No I/O happens here, in `make_charts.build_chart_plan`'s own shape, so a test
    can read a number out of the returned list instead of scraping a rendered
    page. The two return values are the whole contract surface: `report.md` is
    the text, and `metrics_shown` is what makes "every required number is
    present" a set comparison rather than a review habit (CONTRACTS.md §8.6).

    Nothing in this body derives a value. Every number is read from
    `metrics[...]` or `charts[...]` and formatted; §8.4 permits formatting and
    forbids deriving, and the never-recompute rule (§7.3) forbids re-deriving a
    number a frozen document already carries.
    """
    language = str(metrics.get("language") or spec.get("language"))
    # The DOCUMENT language, and the refusal happens here — before a single
    # character of the document exists — so an unsupported language never leaves
    # a half-written report on disk for a reader to find.
    if spec.get("language") != language:
        raise ReportError(
            f"metrics language {language!r} does not match spec language "
            f"{spec.get('language')!r}; the report stage writes one document language"
        )
    tokens = report_tokens(language)
    series_nodes = metrics["series"]
    chart_entries = charts["charts"]
    shown: list[str] = []

    def show(pointer: str) -> None:
        """Record one rendered metric path, first occurrence wins.

        Deduplicating on append is what keeps `metrics_shown` a SET-comparable
        list: the same value is legitimately displayed in more than one section
        (a percentage in «Висновок» and again in «Метрики»), and a list with
        duplicates would make a consumer's set comparison depend on how many
        sections happened to quote it.
        """
        if pointer not in shown:
            shown.append(pointer)

    lines: list[str] = [f"# {tokens['title']}", ""]

    # --- 1. Висновок: the per-series conclusion, each with its own base ---
    lines.append(f"## {tokens['conclusion']}")
    lines.append("")
    for index, node in enumerate(series_nodes):
        label = md_cell(node["label"])
        for window in analyze_trends.GROWTH_WINDOWS:
            phrase = _growth_phrase(tokens, language, node["growth"][window])
            base = format_number(node["avg_daily_views"])
            lines.append(
                f"- {label} · {make_charts.WINDOW_LABELS[window]} "
                f"{tokens['window']}: {phrase} · {tokens['volume_base']} "
                f"{base} {tokens['avg_daily_views']}"
            )
            show(f"series[{index}].growth.{window}.clean.pct")
            show(f"series[{index}].avg_daily_views")
    lines.append("")

    # --- 2. Метрики: the table, carrying as_of and the growth windows ---
    lines.append(f"## {tokens['metrics']}")
    lines.append("")
    lines.append(f"{tokens['as_of']} {metrics['as_of']}")
    lines.append("")
    headers = [
        tokens["series"],
        tokens["project"],
        tokens["article"],
        tokens["language"],
        tokens["period"],
        tokens["total_views"],
        tokens["avg_daily_views"],
        tokens["growth"],
        tokens["direction"],
        tokens["confidence"],
        tokens["anomaly_share"],
    ]
    lines.append("| " + " | ".join(md_cell(header) for header in headers) + " |")
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")
    for index, node in enumerate(series_nodes):
        period = node["period"]
        growth = " / ".join(
            f"{make_charts.WINDOW_LABELS[window]} "
            f"{_growth_phrase(tokens, language, node['growth'][window])}"
            for window in analyze_trends.GROWTH_WINDOWS
        )
        row = [
            node["label"],
            node["project"],
            node["article"],
            node["language"],
            f"{period['start']}..{period['end']} ({format_number(period['days'])})",
            format_number(node["total_views"]),
            format_number(node["avg_daily_views"]),
            growth,
            trend_token(language, str(node["trend_direction"])),
            confidence_token(language, str(node["confidence"])),
            format_number(node["anomaly_share"]),
        ]
        lines.append("| " + " | ".join(md_cell(cell) for cell in row) + " |")
        for field in (
            "label",
            "project",
            "article",
            "language",
            "period.start",
            "period.end",
            "period.days",
            "total_views",
            "avg_daily_views",
            "trend_direction",
            "confidence",
            "anomaly_share",
        ):
            show(f"series[{index}].{field}")
        for window in analyze_trends.GROWTH_WINDOWS:
            show(f"series[{index}].growth.{window}.clean.pct")
    lines.append("")

    # --- 3. Наскільки можна довіряти: the level plus its reasons ---
    lines.append(f"## {tokens['trust']}")
    lines.append("")
    for index, node in enumerate(series_nodes):
        label = md_cell(node["label"])
        reasons = "; ".join(
            md_cell(reason_token(language, reason)) for reason in node["confidence_reasons"]
        )
        level = confidence_token(language, str(node["confidence"]))
        # The comparison below deliberately keeps the RAW enum value: localizing
        # the hypothesis framing would make whether a reading is a hypothesis
        # depend on a translation rather than on the confidence level itself.
        prefix = f"{tokens['hypothesis']}: " if node["confidence"] == "low" else ""
        lines.append(f"- {label}: {prefix}{level} — {reasons}")
        show(f"series[{index}].confidence")
        show(f"series[{index}].confidence_reasons")
    lines.append("")

    # --- 4. Графіки: the 2N+1 inventory, published order, overlay last ---
    lines.append(f"## {tokens['charts']}")
    lines.append("")
    for entry in chart_entries:
        alt = f"{tokens['chart_image_alt']}: {md_cell(entry.get('label', ''))}".strip()
        lines.append(f"![{alt}]({entry['filename']})")
        # The overlay's own disclosure, quoted VERBATIM from the sibling
        # manifest rather than written out again here. charts.v1 §7.2.4 is the
        # reason the two strings differ in general: the manifest note is a stable
        # ASCII interface string, and a second hand-written copy of it is a
        # second sentence that can drift from the first.
        if entry.get("note"):
            lines.append("")
            lines.append(str(entry["note"]))
    lines.append("")

    # --- 5. Обмеження та припущення: only data that already exists ---
    lines.append(f"## {tokens['limitations']}")
    lines.append("")
    assumptions = spec.get("assumptions") or []
    for assumption in assumptions:
        lines.append(f"- {tokens['assumption']}: {md_cell(assumption)}")
    for node in series_nodes:
        note = reason_token(language, str(node["seasonality"]["note"]))
        lines.append(f"- {md_cell(node['label'])}: {md_cell(note)}")
    # The chart stage's own counted disclosures, read rather than recomputed. A
    # reader is told how many days the axis could not draw, because a picture
    # that quietly dropped them without saying so is precisely the silent
    # distortion charts.v1 §7.4 exists to prevent.
    for entry in chart_entries:
        gaps = entry.get("gaps") or []
        gap_days = sum(int(gap.get("days", 0)) for gap in gaps)
        # The `filename` is load-bearing, not decoration: a series' `timeseries`
        # and `growth` entries share one `label`, so a disclosure keyed on the
        # label alone printed the SAME line twice and a reader could not tell
        # which of the two charts it described. `filename` is unique across the
        # whole 2N+1 inventory and §7.2.1 already guarantees it is a safe bare
        # name, so it is both the disambiguator and the join key.
        lines.append(
            f"- {md_cell(entry.get('label', ''))} "
            f"[{md_cell(entry['filename'])}] — "
            f"{tokens['disclosure_gaps']}: {gap_days}; "
            f"{tokens['disclosure_anomalies_drawn']}: {int(entry.get('anomalies_drawn', 0))}; "
            f"{tokens['disclosure_log_masked_points']}: "
            f"{int(entry.get('log_masked_points', 0))}"
        )
    lines.append("")

    # --- 6. Наступний крок: a fixed, non-numeric localized line ---
    lines.append(f"## {tokens['next_step']}")
    lines.append("")
    # No ranking, no count, no number: REC-01's cross-series ranking is a v2
    # requirement, and D-03 forbids a cross-series rollup anywhere in the
    # metrics lineage, so the honest v1 next step is a statement of what this
    # document is rather than a recommendation derived from a rollup.
    lines.append(f"{tokens['not_a_forecast']}.")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        f"{tokens['source']}: {md_cell(metrics['spec_name'])} · "
        f"{tokens['as_of']} {metrics['as_of']} · {tokens['measured']} · "
        f"{tokens['not_a_forecast']}"
    )
    lines.append("")

    return "\n".join(lines), shown


def dump_text(text: str, path: str | Path) -> None:
    """Atomically write UTF-8 text through a same-directory staging file.

    `common.dump_json`'s discipline exactly, in `dump_json`'s own words, because
    the guarantee is the same one: a publication failure leaves the prior bytes
    untouched and leaves no `report.md.*.tmp` behind. `dump_json` itself cannot
    be reused — it hard-codes `json.dumps` — so this is its deliberate twin
    rather than a second implementation with different discipline.

    `newline="\\n"` is inherited on purpose: a report written with the platform's
    newline would carry CRLF into a file a Linux reader and a Windows reader then
    disagree about byte-for-byte, and the digest the next run takes would depend
    on the machine that produced it.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=p.parent,
            prefix=f".{p.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(text)
            handle.flush()
        os.replace(temporary_path, p)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _manifest(
    *,
    spec_name: str,
    as_of: str,
    language: str,
    metrics_path: str,
    metrics_sha256: str,
    charts_sha256: str,
    metrics_shown: Sequence[str],
) -> dict[str, object]:
    """Serialize the ten ratified keys, in the order CONTRACTS.md §8.1 lists them.

    `formats` is an array of OBJECTS, `[{format, filename}]`, not an array of
    bare strings. That shape is the whole reason a v1.x PDF is one appended entry
    and one more file rather than a rename of the Markdown slot, so it is frozen
    here rather than left to a later phase to discover.
    """
    return {
        "contract_version": REPORT_CONTRACT_VERSION,
        "spec_name": spec_name,
        "as_of": as_of,
        "language": language,
        "generated_from": metrics_path,
        "metrics_sha256": metrics_sha256,
        "charts_sha256": charts_sha256,
        "report_filename": REPORT_FILENAME,
        "metrics_shown": list(metrics_shown),
        "formats": [{"format": "markdown", "filename": REPORT_FILENAME}],
    }


def report_is_stale(out_dir: str | Path) -> bool:
    """Is the report in `out_dir` no longer rendered from the documents on disk?

    **A digest comparison, and a recorded deviation from the roadmap's wording.**
    ROADMAP Phase 6 success criterion 4 describes staleness as "when
    `metrics.json` is newer than the report"; this function compares the
    SHA-256 of the two input documents' BYTES against the `metrics_sha256` and
    `charts_sha256` the manifest already publishes. The reconciliation is
    CONTRACTS.md §7.1's, applied to the sibling manifest, where a file's
    modification time was rejected with the sentence that an mtime carries no
    information about *which* content was rendered; §8.1 records the same
    deviation for this stage. A timestamp comparison fails in both directions:
    it reports "fresh" for a report rendered from entirely different content the
    moment that content is copied or checked out with a new timestamp, and it
    reports "stale" for byte-identical content that merely moved. So a consumer
    here decides from WHICH CONTENT was rendered, never from when a file was
    touched, and the module test
    `test_report_module_never_reads_a_timestamp` enforces that mechanically
    rather than trusting this docstring.

    The two digests are consumed, never recomputed from a second read: the
    manifest's own values are the whole comparison, so this seam reads the
    inputs exactly once and cannot attest to content a different read saw. And
    they are an **integrity and staleness mechanism only** — they detect that
    content changed, they authenticate nothing, and no later phase may gate
    access on them (§8.1's own sentence).

    Every unanswerable state returns True rather than raising. A consumer asking
    "is this report current?" must get an answer when the answer is plainly no:
    a missing, unreadable, malformed, non-object or key-incomplete manifest, and
    a missing or unreadable input, all mean the manifest cannot be shown to
    describe what is on disk. Raising would push that decision back onto a
    caller, and a caller that guesses is how a stale report gets presented as
    current.
    """
    directory = Path(out_dir)
    try:
        manifest = json.loads((directory / REPORT_MANIFEST_FILENAME).read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return True
    if not isinstance(manifest, dict):
        return True
    recorded = (manifest.get("metrics_sha256"), manifest.get("charts_sha256"))
    if not all(isinstance(value, str) for value in recorded):
        return True
    for name, expected in zip(("metrics.json", "charts.json"), recorded):
        try:
            raw = (directory / name).read_bytes()
        except OSError:
            return True
        if hashlib.sha256(raw).hexdigest() != expected:
            return True
    return False


def _parser() -> argparse.ArgumentParser:
    """Exactly three flags. A fourth would be a contract Phase 7 must learn to pass."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, help="path to spec.json")
    parser.add_argument("--out", default="out", help="output directory (default: out)")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="logging-only debug diagnostics; adds no manifest field and changes no behavior",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the local-only report CLI."""
    args = _parser().parse_args(argv)
    setup_logging(args.verbose)
    # Uncaught on purpose: SystemExit(2) is inherited verbatim from
    # `load_and_validate_spec` and never intercepted here, so a spec problem is
    # indistinguishable at the call site from a spec problem in any other stage.
    spec = load_and_validate_spec(args.spec)
    out_dir = Path(args.out)
    metrics_path = out_dir / "metrics.json"
    charts_path = out_dir / "charts.json"
    report_path = out_dir / REPORT_FILENAME
    manifest_path = out_dir / REPORT_MANIFEST_FILENAME
    try:
        metrics, metrics_sha256 = make_charts.load_metrics(metrics_path)
        charts_document, charts_sha256 = load_charts(charts_path)
        require_display_fields(metrics)
        text, metrics_shown = render_report(spec, metrics, charts_document)
        # report.md FIRST, and the manifest only after it is in place, so a
        # manifest never names a report that is not there (§8.7).
        try:
            dump_text(text, report_path)
            dump_json(
                _manifest(
                    spec_name=str(metrics["spec_name"]),
                    as_of=str(metrics["as_of"]),
                    language=str(spec["language"]),
                    metrics_path=str(metrics_path),
                    metrics_sha256=metrics_sha256,
                    charts_sha256=charts_sha256,
                    metrics_shown=metrics_shown,
                ),
                manifest_path,
            )
        except OSError as error:
            raise ReportError(f"could not write report output: {report_path}") from error
    except (ReportError, make_charts.ChartError, analyze_trends.AnalysisError) as error:
        print(f"report failed: {error}", file=sys.stderr)
        return 1
    print(f"Wrote report: {report_path}; output: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
