"""Render local Wikipedia pageview metrics into Agg PNGs and a charts.json manifest.

The stage is split in two halves on purpose (RESEARCH Pattern 1). The planning
half is pure: `build_chart_plan` reads `metrics.json` + `series.csv` and
returns frozen dataclasses holding *every* number that will reach a chart, so
a test can read the number instead of guessing at pixels. The rendering half is
dumb: `render_chart` maps those fields onto matplotlib calls and performs no
arithmetic. matplotlib is imported *inside* the render function only, so
importing this module never pulls a rendering backend (C-03 / RESEARCH §
Environment: this machine's default backend is `tkagg`, not Agg).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any

import analyze_trends
import common
from common import dump_json, load_and_validate_spec, setup_logging

log = common.log

# The chart stage is the first stage to turn a spec string into a filesystem
# name (T-5-01). `common.validate_spec` accepts any non-empty string as a
# series id, so the filename guard is a new, local, mandatory check.
SERIES_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
# D-06/D-07: the smoothed line reuses the anomaly detector's own window radius,
# so the drawn median can never disagree with the `clean` growth it twins.
MEDIAN_RADIUS_DAYS = analyze_trends.MAD_RADIUS_DAYS
FIGURESIZE = (9.0, 3.6)  # AGENTS.md § Charting
DPI = 150
# D-14: the timeseries floor is the literal 0.0; the ceiling only adds headroom
# so the highest raw point is not drawn on the frame.
Y_HEADROOM_FACTOR = 1.05
TIMESERIES_KIND = "timeseries"
GROWTH_KIND = "growth"
OVERLAY_KIND = "overlay"
# The overlay is owned by no single series, so its name is fixed rather than
# derived from a series_id - which is also what keeps it from ever colliding
# with a per-series name (T-5-09).
OVERLAY_FILENAME = "chart_overlay.png"
REASON_NA_LABEL = "n/a"  # D-04: a null growth renders as a labelled n/a bar
# Provisional until plan 05-07 ratifies the charts.v1 field list in
# CONTRACTS.md §7 (D-18).
CHARTS_CONTRACT_VERSION = "charts.v1"
# D-09: the method disclosure a chart carries so it stays self-describing when
# it travels on its own. The wording is a numeric description of the two drawn
# series, not prose, so it is language-neutral; the *label* is spec-authored in
# the requested language and is copied verbatim (D-16).
METHOD_PHRASE = "raw daily / 7-day median"
RAW_LINE_LABEL = "raw daily"
MEDIAN_LINE_LABEL = "7-day median"
# D-20: the overlay's disclosure. A constant rather than a formatted sentence, so
# a test can assert it exactly and Phase 6 can quote it verbatim.
OVERLAY_NOTE = "comparative view; per-series scales differ"
# The overlay owns no single series and so has no metrics `label` to copy. This
# constant names the comparison view itself - it is a manifest display name, not
# a metrics value, and the per-series names are not lost to it: the legend
# carries each SeriesLine.label verbatim.
OVERLAY_LABEL = "all series compared"
# The overlay draws one raw line per series, so it names its own method with its
# own phrase rather than the per-series one: claiming a 7-day median on a picture
# that carries no median line would be the exact misrepresentation this phase
# exists to prevent.
OVERLAY_METHOD_PHRASE = "raw daily; shared y-axis"
# A distinct colour per series, so two lines in the comparison view never share
# one. Read off the entry's own position, never from a matplotlib cycle that
# could resynchronize between charts.
OVERLAY_COLOR_CYCLE = ("#1c7ed6", "#f08c00", "#2f9e44", "#d6336c", "#7048e8", "#0c8599")
# D-02: the growth chart carries all three windows, not only the 1Y CHRT-01 names.
# The order mirrors analyze_trends.GROWTH_WINDOWS' own key order, so the bar
# order is the producer's contract order rather than an arbitrary one - and
# `test_growth_bar_order_matches_contract_key_order` pins that equality.
GROWTH_BAR_WINDOWS = ("m3", "y1", "y2")
# D-05: the reader never maps a tick position to a window by counting rows; the
# window names itself. These are display tokens, not metrics values, so no
# compared number is affected by them.
WINDOW_LABELS = {"m3": "3M", "y1": "1Y", "y2": "2Y"}
# D-09 on the growth chart: the method this chart draws is the clean variant, so
# the subtitle names that rather than reusing the timeseries' raw/median phrase.
GROWTH_METHOD_PHRASE = "clean growth (anomalies excluded)"
# D-04: a null growth is a visible, labelled, hatched bar - never a missing row
# and never a zero-height solid one.
NULL_BAR_COLOR = "#ADB5BD"
NULL_BAR_HATCH = "///"
# Bar geometry. `barh` takes a *height* argument, so BAR_HEIGHT is the vertical
# extent of each horizontal bar - it is not a `set_height` call, which is the
# vertical-form call the prohibition names.
BAR_HEIGHT = 0.55
# The drawn value of a non-null growth bar. Colour is display-only; the compared
# number lives in `GrowthBar.pct` and is never touched by a style choice.
GROWTH_BAR_COLOR = "#1c7ed6"
# D-14 is a timeseries/overlay rule only. The growth chart's value axis is a
# percentage axis, so its bounds come from the plan's own y_limits (which always
# contain 0.0) and the branch below names no axis-limit call at all - see the
# AST guard `test_growth_axes_never_receive_a_zero_floor`.
GROWTH_Y_PAD_FACTOR = 0.10
# The all-null fallback range, so three hatched bars are still renderable when
# no window is computable at all.
GROWTH_ALL_NULL_LIMITS = (0.0, 1.0)


class ChartError(RuntimeError):
    """A model-readable local input or rendering failure."""


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    """One plotted observation: a calendar day and the views recorded for it."""

    date: date
    views: int | float


@dataclass(frozen=True, slots=True)
class GrowthBar:
    """One growth window's plotted value, copied from metrics.json (D-02/D-03)."""

    window: str
    pct: float | None
    abs: int | None
    base_avg_daily_views: float
    reason: str | None


@dataclass(frozen=True, slots=True)
class SeriesLine:
    """One series' render data on the comparison view, copied from its own entry.

    Render-only, and never serialized into charts.json: the overlay already
    publishes its membership through `series_ids` and the per-series entries it
    references, so emitting the nested per-series data would duplicate
    metrics.json inside the manifest for no consumer.
    """

    series_id: str
    label: str
    points: tuple[SeriesPoint, ...]
    raw_values: tuple[int | float, ...]
    median_values: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class ChartEntry:
    """Every number and label that reaches one PNG; the renderer adds nothing."""

    kind: str
    series_id: str | None
    # The series' position in spec.series[], or None for a chart that belongs to
    # no single spec position (the overlay). Emitted in the manifest so the
    # ordering is inspectable rather than merely implied by list position.
    spec_index: int | None
    # Which series a chart shows. For the two per-series kinds that is the one
    # series itself; for the overlay it is the full membership, in metrics order.
    series_ids: tuple[str, ...]
    # Render-only per-series data for the overlay; always () elsewhere and never
    # serialized (see SeriesLine).
    series_lines: tuple[SeriesLine, ...]
    label: str
    language: str
    filename: str
    points: tuple[SeriesPoint, ...]
    raw_values: tuple[int | float, ...]
    median_values: tuple[float, ...]
    gaps: tuple[tuple[date, date], ...]
    anomalies: tuple[dict[str, object], ...]
    anomalies_drawn: int
    yscale: str
    y_limits: tuple[float, float]
    bars: tuple[GrowthBar, ...]
    note: str | None
    subtitle: str


@dataclass(frozen=True, slots=True)
class ChartDocument:
    """The whole chart inventory plus the digest of the metrics it was built from."""

    contract_version: str
    spec_name: str
    as_of: str
    language: str
    generated_from: str
    metrics_sha256: str
    charts: tuple[ChartEntry, ...]


def _require_str(container: Mapping[str, Any], key: str, where: str) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value:
        raise ChartError(f"{where}.{key} must be a non-empty string")
    return value


def _require_number(container: Mapping[str, Any], key: str, where: str) -> int | float:
    value = container.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ChartError(f"{where}.{key} must be a number")
    return value


def load_metrics(path: str | Path) -> tuple[dict[str, object], str]:
    """Read, shape-check, and hash the local metrics.json in a single pass.

    The digest is taken from the exact bytes read, so a consumer can detect a
    stale manifest without ever comparing timestamps.
    """
    metrics_path = Path(path)
    try:
        raw = metrics_path.read_bytes()
    except OSError as error:
        raise ChartError(f"could not read metrics JSON: {metrics_path}") from error
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ChartError(f"metrics JSON is malformed: {metrics_path}") from error
    if not isinstance(document, dict):
        raise ChartError("metrics JSON top level must be an object")
    _require_str(document, "spec_name", "metrics")
    _require_str(document, "as_of", "metrics")
    series = document.get("series")
    if not isinstance(series, list) or not series:
        raise ChartError("metrics.series must be a non-empty list")
    for index, item in enumerate(series):
        where = f"metrics.series[{index}]"
        if not isinstance(item, dict):
            raise ChartError(f"{where} must be an object")
        _require_str(item, "series_id", where)
        _require_str(item, "label", where)
        _require_str(item, "language", where)
        _require_number(item, "avg_daily_views", where)
        growth = item.get("growth")
        if not isinstance(growth, dict):
            raise ChartError(f"{where}.growth must be an object")
        for window in analyze_trends.GROWTH_WINDOWS:
            if window not in growth:
                # The same wording the growth bar builder raises, so a missing
                # window reports identically whichever guard reaches it first.
                raise ChartError(
                    f"metrics series is missing growth window {window!r}: {item['series_id']}"
                )
        if not isinstance(item.get("anomalies"), list):
            raise ChartError(f"{where}.anomalies must be a list")
    # Reuse the analyzer's finite walk rather than writing a second one: a
    # `1e400` or a NaN must fail closed before it can be drawn as a silent lie
    # (T-5-02).
    try:
        analyze_trends.validate_finite_numbers(document)
    except analyze_trends.AnalysisError as error:
        raise ChartError(f"metrics JSON carries a non-finite number: {error}") from error
    return document, hashlib.sha256(raw).hexdigest()


def rolling_median_7(values: Sequence[int | float]) -> list[float]:
    """Centered 7-day rolling median; the window is truncated at the series edges."""
    smoothed: list[float] = []
    for index in range(len(values)):
        window = values[
            max(0, index - MEDIAN_RADIUS_DAYS) : index + MEDIAN_RADIUS_DAYS + 1
        ]
        smoothed.append(float(median(window)))
    return smoothed


def series_filename(series_id: str, kind: str) -> str:
    """Return the deterministic PNG name for a series, refusing any path escape."""
    if not SERIES_ID_PATTERN.fullmatch(series_id):
        raise ChartError(f"series id is not filename-safe: {series_id!r}")
    return f"chart_{series_id}_{kind}.png"


def _subtitle(label: str) -> str:
    """D-09: the chart names its own method, next to the spec-authored label."""
    return f"{label} - {METHOD_PHRASE}"


def _spec_series_index(spec: Mapping[str, Any]) -> dict[str, int]:
    """Map every spec.series[] id to its position, so chart order is inspectable."""
    series = spec.get("series")
    if not isinstance(series, list) or not series:
        raise ChartError("spec.series must be a non-empty list")
    index_by_id: dict[str, int] = {}
    for position, item in enumerate(series):
        where = f"spec.series[{position}]"
        if not isinstance(item, dict):
            raise ChartError(f"{where} must be an object")
        series_id = _require_str(item, "id", where)
        if series_id in index_by_id:
            raise ChartError(f"duplicate spec series id: {series_id}")
        index_by_id[series_id] = position
    return index_by_id


def _timeseries_entry(
    node: Mapping[str, Any],
    observations: Sequence[analyze_trends.Observation],
    spec_index: int,
) -> ChartEntry:
    """Build the one timeseries entry a series owns; plan 05-04 adds the growth sibling."""
    series_id = _require_str(node, "series_id", "metrics.series entry")
    label = _require_str(node, "label", "metrics.series entry")
    points = tuple(SeriesPoint(observation.date, observation.views) for observation in observations)
    raw_values = tuple(point.views for point in points)
    # D-14: the floor is the literal 0.0, never a computed value, so no axis can
    # ever imply negative views.
    return ChartEntry(
        kind=TIMESERIES_KIND,
        series_id=series_id,
        spec_index=spec_index,
        series_ids=(series_id,),
        series_lines=(),
        label=label,
        language=_require_str(node, "language", "metrics.series entry"),
        filename=series_filename(series_id, TIMESERIES_KIND),
        points=points,
        raw_values=raw_values,
        median_values=tuple(rolling_median_7(raw_values)),
        gaps=(),
        anomalies=(),
        anomalies_drawn=0,
        yscale="linear",
        y_limits=(0.0, float(max(raw_values)) * Y_HEADROOM_FACTOR),
        bars=(),
        note=None,
        subtitle=_subtitle(label),
    )


def _growth_bars(
    node: Mapping[str, Any], series_id: str
) -> tuple[GrowthBar, ...]:
    """Copy the three growth windows out of metrics.json, verbatim.

    D-03: the plotted number is `growth.<window>.clean.pct` and only that - the
    anomaly-replaced value trend_direction and confidence are built on. The raw
    `pct` is never read, so a spike cannot re-enter the chart as if it were the
    finding. Every field is copied, not re-derived: `base_avg_daily_views` is
    the metrics series' own `avg_daily_views` (T-5-13), never a mean recomputed
    from series.csv.

    Two fail-closed guards, both mandatory:
    - an absent window raises, because D-02 makes a missing 2Y itself signal
      and a chart cannot show the difference;
    - a null pct with no reason raises, because a bar that says "we don't know"
      without saying why is exactly the silent omission D-04 forbids.
    """
    series_id = _require_str(node, "series_id", "metrics.series entry")
    growth = node.get("growth")
    if not isinstance(growth, Mapping):
        raise ChartError(f"metrics series has no growth object: {series_id}")
    base = float(_require_number(node, "avg_daily_views", "metrics.series entry"))
    bars: list[GrowthBar] = []
    for window in GROWTH_BAR_WINDOWS:
        window_node = growth.get(window)
        if not isinstance(window_node, Mapping):
            raise ChartError(
                f"metrics series is missing growth window {window!r}: {series_id}"
            )
        clean = window_node.get("clean")
        if not isinstance(clean, Mapping):
            raise ChartError(
                f"growth {window} has no clean object for series: {series_id}"
            )
        # Copied verbatim, never rounded and never re-derived: this object is
        # the number metrics.json computed, and the test compares it with `==`.
        pct = clean.get("pct")
        if pct is not None and (isinstance(pct, bool) or not isinstance(pct, (int, float))):
            raise ChartError(
                f"growth {window} clean.pct must be a number or null for series: {series_id}"
            )
        pct_value = None if pct is None else float(pct)
        reason: str | None = None
        if pct_value is None:
            reason_value = clean.get("reason")
            if not isinstance(reason_value, str) or not reason_value:
                raise ChartError(
                    f"growth {window} is null without a reason for series: {series_id}"
                )
            reason = reason_value
        abs_value = clean.get("abs")
        bars.append(
            GrowthBar(
                window=window,
                pct=pct_value,
                # `abs` is null exactly when the pct is null; never 0 (D-04/ANAL-06).
                abs=None if pct_value is None else abs_value,
                base_avg_daily_views=base,
                reason=reason,
            )
        )
    return tuple(bars)


def _growth_value_limits(bars: Sequence[GrowthBar]) -> tuple[float, float]:
    """The growth chart's value-axis display bounds, computed to always contain 0.0.

    RESEARCH Pitfall 1 is the reason this function exists. A shared
    "anchor every axis at zero" helper (D-14 read as `set_ylim(0, None)`)
    produced `ylim (0.0, 18.63)` on the values `[3.5, 16.7, -22.0]`, putting the
    whole decline outside the visible axis - a falling topic drawn as a small
    rising one. `min(0.0, ...)` on the floor is the structural fix: zero is
    always inside the range, so a negative bar is always drawable. This is the
    *value* axis, which on the horizontal growth form is x - the field keeps its
    contract name for continuity with the timeseries and overlay entries.
    """
    values = [bar.pct for bar in bars if bar.pct is not None]
    if not values:
        return GROWTH_ALL_NULL_LIMITS
    y_min = min(0.0, min(values))
    y_max = max(0.0, max(values))
    span = y_max - y_min
    if span <= 0.0:
        return (y_min, y_max if y_max > y_min else y_min + 1.0)
    pad = span * GROWTH_Y_PAD_FACTOR
    return (y_min - pad, y_max + pad)


def _growth_entry(node: Mapping[str, Any], spec_index: int) -> ChartEntry:
    """Build the one growth entry a series owns, alongside its timeseries sibling.

    Every plotted value is copied from metrics.json by `_growth_bars`; this body
    only decides the display bounds and the labels around them.
    """
    series_id = _require_str(node, "series_id", "metrics.series entry")
    label = _require_str(node, "label", "metrics.series entry")
    bars = _growth_bars(node, series_id)
    return ChartEntry(
        kind=GROWTH_KIND,
        series_id=series_id,
        spec_index=spec_index,
        series_ids=(series_id,),
        series_lines=(),
        label=label,
        language=_require_str(node, "language", "metrics.series entry"),
        filename=series_filename(series_id, GROWTH_KIND),
        # The growth chart draws bars, not a daily series: no points, so the
        # manifest reports 0 plotted observations rather than borrowing the
        # timeseries' count.
        points=(),
        raw_values=(),
        median_values=(),
        gaps=(),
        anomalies=(),
        anomalies_drawn=0,
        yscale="linear",
        y_limits=_growth_value_limits(bars),
        bars=bars,
        note=None,
        subtitle=f"{label} - {GROWTH_METHOD_PHRASE}",
    )


def _overlay_entry(
    per_series: Sequence[ChartEntry], spec_language: str
) -> ChartEntry:
    """Build the one comparison view a document publishes, last in charts[].

    D-20: one shared *raw* y-axis, never a normalized or rebased one. Each line
    reuses its series' own already-built values, so the comparison view cannot
    introduce a number with no source in metrics.json.
    """
    if not per_series:
        raise ChartError("the overlay needs at least one per-series chart")
    lines = tuple(
        SeriesLine(
            series_id=entry.series_id or "",
            label=entry.label,
            points=entry.points,
            raw_values=entry.raw_values,
            median_values=entry.median_values,
        )
        for entry in per_series
    )
    # D-14 applied to the overlay as a timeseries axes - which is what it is.
    # One shared ceiling over every series, so the axis is comparable.
    largest = max(max(line.raw_values) for line in lines)
    return ChartEntry(
        kind=OVERLAY_KIND,
        series_id=None,
        spec_index=None,
        series_ids=tuple(line.series_id for line in lines),
        series_lines=lines,
        label=OVERLAY_LABEL,
        # The document-level language, never one series' language: the overlay
        # spans them all, and Phase 6 must be able to tell a Ukrainian overlay
        # from an English one without reading pixels.
        language=spec_language,
        filename=OVERLAY_FILENAME,
        # The overlay owns no single series' point tuple; its plotted data lives
        # in series_lines, which the manifest reports as a count only.
        points=(),
        raw_values=(),
        median_values=(),
        gaps=(),
        anomalies=(),
        anomalies_drawn=0,
        yscale="linear",
        y_limits=(0.0, float(largest) * Y_HEADROOM_FACTOR),
        bars=(),
        note=OVERLAY_NOTE,
        subtitle=f"{OVERLAY_LABEL} - {OVERLAY_METHOD_PHRASE}",
    )


def build_chart_plan(
    spec: Mapping[str, Any],
    metrics: Mapping[str, Any],
    metrics_sha256: str,
    grouped: Mapping[str, Sequence[analyze_trends.Observation]],
    metrics_path: str,
) -> ChartDocument:
    """Decide every number that will be drawn, without importing a backend.

    Growth, totals, averages, and anomaly local medians are read from
    `metrics.json` and never recomputed (ANAL-06 / CHRT-02). The single
    permitted derivation is the centered rolling median, and it never feeds a
    numeric label.
    """
    language = _require_str(spec, "language", "spec")
    series_nodes = metrics["series"]
    if not isinstance(series_nodes, list) or not series_nodes:
        raise ChartError("metrics.series must be a non-empty list")
    # A chart is identified by the pair (series_id, kind). D-19's 2N+1 inventory
    # - one PNG per pair, plus the one overlay - is what that identity produces
    # once both per-series kinds exist. Iteration follows metrics.series document
    # order, which CONTRACTS.md §3 freezes as spec.series[] order; nothing here
    # sorts by label, language or views.
    spec_index_by_id = _spec_series_index(spec)
    charts: list[ChartEntry] = []
    for node in series_nodes:
        if not isinstance(node, dict):
            raise ChartError("metrics.series entries must be objects")
        series_id = _require_str(node, "series_id", "metrics.series entry")
        # The stage joins the two documents rather than trusting either one: a
        # metrics series the spec does not declare must never be labelled with
        # some other series' language or article (T-5-02).
        if series_id not in spec_index_by_id:
            raise ChartError(f"metrics series is not in spec: {series_id}")
        series_filename(series_id, TIMESERIES_KIND)
        observations = list(grouped.get(series_id, []))
        if not observations:
            raise ChartError(f"no observations for series: {series_id}")
        # D-19: one PNG per (series_id, kind). The two per-series kinds are
        # emitted as siblings, adjacent, in metrics.series order - the growth
        # chart is not a separate list and never sorts away from its series.
        charts.append(_timeseries_entry(node, observations, spec_index_by_id[series_id]))
        charts.append(_growth_entry(node, spec_index_by_id[series_id]))
    # D-01/D-19: the comparison view is a *separate* chart, appended last. It
    # never replaces or suppresses the per-series charts, because a per-series
    # view and a comparison answer different questions. It is fed the
    # *timeseries* entries only: a growth entry carries no daily series, so
    # including it would contribute an empty line to the shared axis.
    charts.append(
        _overlay_entry([c for c in charts if c.kind == TIMESERIES_KIND], language)
    )
    return ChartDocument(
        contract_version=CHARTS_CONTRACT_VERSION,
        spec_name=_require_str(metrics, "spec_name", "metrics"),
        as_of=_require_str(metrics, "as_of", "metrics"),
        language=language,
        generated_from=str(metrics_path),
        metrics_sha256=metrics_sha256,
        charts=tuple(charts),
    )


def _draw_timeseries(entry: ChartEntry, ax: Any, mdates: Any) -> None:
    """Draw one series: a thin raw daily line under a bold 7-day median line.

    D-07 keeps the raw line visible so an anomaly marker lands on a line the
    reader can see. D-14's zero floor is a timeseries rule and is applied here,
    off the entry's own limits.
    """
    x_values = mdates.date2num([point.date for point in entry.points])
    ax.plot(x_values, entry.raw_values, lw=0.6, alpha=0.55, color="#4C6EF5", label=RAW_LINE_LABEL)
    ax.plot(
        x_values, entry.median_values, lw=1.8, color="#212529", label=MEDIAN_LINE_LABEL
    )
    # D-14: an explicit zero floor, timeseries axes only. A growth chart must
    # never receive this call - it would erase a negative bar (RESEARCH
    # Pitfall 1).
    ax.set_ylim(entry.y_limits[0], entry.y_limits[1])
    # D-10: the axis spans the full calendar range; only rows present in
    # series.csv are plotted, and a views=0 row is drawn at face value (D-11).
    ax.xaxis_date()
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.legend(loc="upper left", frameon=False, fontsize=8)


def _draw_overlay(entry: ChartEntry, ax: Any, mdates: Any) -> None:
    """Draw every series on one shared raw y-axis, plus the scales-differ note.

    D-20 forbids a log or normalized scale here: that would invent a per-series
    index with no source in metrics.json. The x-axis is left to matplotlib's
    autoscale, which spans exactly the union of the series' plotted date ranges
    (D-10) - no `set_xlim` call, so no invented bound.
    """
    for index, line in enumerate(entry.series_lines):
        x_values = mdates.date2num([point.date for point in line.points])
        ax.plot(
            x_values,
            line.raw_values,
            lw=0.9,
            alpha=0.8,
            color=OVERLAY_COLOR_CYCLE[index % len(OVERLAY_COLOR_CYCLE)],
            label=line.label,
        )
    ax.set_ylim(entry.y_limits[0], entry.y_limits[1])
    ax.xaxis_date()
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    # D-09: the disclosure is drawn into the image, so a PNG separated from the
    # report still says the scales are not comparable. It sits above the legend
    # so the two never overlap.
    if entry.note:
        ax.annotate(
            entry.note,
            xy=(0.0, 1.0),
            xycoords="axes fraction",
            xytext=(4, -8),
            textcoords="offset points",
            ha="left",
            va="top",
            fontsize=8,
            color="#495057",
        )
    ax.legend(loc="upper left", bbox_to_anchor=(0.0, 0.92), frameon=False, fontsize=8)


def _draw_growth(entry: ChartEntry, ax: Any) -> None:
    """Draw one series' growth: three horizontal bars, one per window.

    Orientation follows CHRT-01 and D-02 - `ax.barh`, never `ax.bar`, and no
    `set_height` (the vertical-form call). The percentage is therefore the *x*
    (value) axis and the window is the y (category) axis.

    The single most important property of this body is what it does NOT call:
    there is no `set_xlim`, no `set_ylim` and no `set_xscale` here, so the value
    axis can never receive a zero floor. On this horizontal form the footgun is
    `set_xlim(left=0)`, which RESEARCH Pitfall 1 measured deleting a -22.0%
    decline from the picture entirely. The bounds come from the plan's
    `entry.y_limits`, which already contains 0.0 and any negative bar, and the
    zero reference is drawn explicitly below so a decline reads as a decline.
    `test_growth_axes_never_receive_a_zero_floor` walks this branch by AST to
    keep it that way.
    """
    positions = list(range(len(entry.bars)))
    labels: list[str] = []
    for index, bar in enumerate(entry.bars):
        window_label = WINDOW_LABELS[bar.window]
        labels.append(window_label)
        if bar.pct is None:
            # D-04: visible, hatched, grey, and annotated with the contract
            # reason. Never a missing row, never a zero-height solid bar.
            drawn = ax.barh(index, 0.0, height=BAR_HEIGHT)
            for patch in drawn:
                patch.set_color(NULL_BAR_COLOR)
                patch.set_hatch(NULL_BAR_HATCH)
            ax.annotate(
                f"{REASON_NA_LABEL}\n{bar.reason}",
                xy=(0.0, index),
                xytext=(6, 0),
                textcoords="offset points",
                va="center",
                ha="left",
                fontsize=8,
                color="#495057",
            )
            continue
        drawn = ax.barh(index, bar.pct, height=BAR_HEIGHT)
        for patch in drawn:
            patch.set_color(GROWTH_BAR_COLOR)
        # D-05: value, window and volume base on the chart itself, so every
        # dynamic number is traceable without the report. The base is formatted
        # with a space thousands separator (display-only; the compared value in
        # charts.json is the unformatted contract number).
        ax.annotate(
            f"{window_label} {bar.pct:+.1f}%\non {bar.base_avg_daily_views: ,.1f} views/day",
            xy=(bar.pct, index),
            xytext=(6 if bar.pct >= 0 else -6, 0),
            textcoords="offset points",
            va="center",
            ha="left" if bar.pct >= 0 else "right",
            fontsize=8,
            color="#212529",
        )
    ax.set_yticks(positions)
    ax.set_yticklabels(labels, fontsize=8)
    # The zero reference, drawn explicitly: without it a small negative bar next
    # to a large positive one is easy to read as "no change".
    ax.axvline(0, color="#212529", lw=0.8)


def render_chart(entry: ChartEntry, out_dir: Path) -> Path:
    """Draw one chart from its plan entry. No arithmetic happens in this body."""
    import matplotlib

    matplotlib.use("Agg")  # must precede pyplot: this machine defaults to tkagg
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    out_dir = Path(out_dir)
    path = out_dir / entry.filename
    if path.resolve().parent != out_dir.resolve():
        raise ChartError(f"chart target escapes the output directory: {entry.filename}")
    fig, ax = plt.subplots(figsize=FIGURESIZE, dpi=DPI)
    if entry.kind == GROWTH_KIND:
        _draw_growth(entry, ax)
    elif entry.kind == OVERLAY_KIND:
        _draw_overlay(entry, ax, mdates)
    else:
        _draw_timeseries(entry, ax, mdates)
    ax.set_title(entry.subtitle, fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=DPI)
    plt.close(fig)
    return path


def _bar_payload(bar: GrowthBar) -> dict[str, object]:
    """Serialize one growth bar under the charts.v1 omission rule.

    `pct`, `abs` and `reason` are omitted when the clean pct is null and are
    never written as null - the same no-null rule 05-02 froze. `pct` belongs in
    that clause for the same reason as the other two: it is `float | None`, and
    the committed golden has `y2.clean.pct` null on every run, so writing it
    would put a literal null in the manifest. The documented `bar_null` boolean
    is how a consumer reads "not computable" from a field rather than from a
    key's absence, and it is what keeps a real 0.0% reading structurally
    distinct from a null one.
    """
    payload: dict[str, object] = {
        "window": bar.window,
        "label": WINDOW_LABELS[bar.window],
        "base_avg_daily_views": bar.base_avg_daily_views,
        "bar_null": bar.pct is None,
    }
    if bar.pct is not None:
        payload["pct"] = bar.pct
    if bar.abs is not None:
        payload["abs"] = bar.abs
    if bar.reason is not None:
        payload["reason"] = bar.reason
    return payload


def _plotted_point_count(entry: ChartEntry) -> int:
    """How many observations this chart draws.

    A count of what was plotted, never a data value. The overlay owns no single
    series' point tuple - its data lives in `series_lines` - so the count is the
    sum of the per-series lines it actually draws.
    """
    if entry.kind == OVERLAY_KIND:
        return sum(len(line.points) for line in entry.series_lines)
    return len(entry.points)


def _entry_payload(entry: ChartEntry) -> dict[str, object]:
    """Serialize one chart entry under the charts.v1 omission rule.

    A key is written when its value is meaningful for this entry and omitted -
    never written as null - when it is not. `gaps` and `anomalies_drawn` are
    unconditional so a consumer never special-cases an absent one.
    """
    payload: dict[str, object] = {
        "kind": entry.kind,
        "label": entry.label,
        "language": entry.language,
        "filename": entry.filename,
        "yscale": entry.yscale,
        "y_limits": [entry.y_limits[0], entry.y_limits[1]],
        "points": _plotted_point_count(entry),
        "gaps": [[start.isoformat(), end.isoformat()] for start, end in entry.gaps],
        "anomalies_drawn": entry.anomalies_drawn,
        "subtitle": entry.subtitle,
    }
    if entry.series_id is not None:
        payload["series_id"] = entry.series_id
    # Same omission rule as `series_id`: written only by the kinds that own it.
    # The overlay belongs to no spec.series[] position, so it carries none.
    if entry.spec_index is not None:
        payload["spec_index"] = entry.spec_index
    # `series_ids` belongs to the overlay, the one chart that spans every series.
    if entry.kind == OVERLAY_KIND:
        payload["series_ids"] = list(entry.series_ids)
    if entry.kind == GROWTH_KIND and entry.bars:
        payload["bars"] = [_bar_payload(bar) for bar in entry.bars]
    if entry.note is not None:
        payload["note"] = entry.note
    return payload


def _manifest(document: ChartDocument) -> dict[str, object]:
    """Serialize the plan: the manifest cannot disagree with the picture."""
    return {
        "contract_version": document.contract_version,
        "spec_name": document.spec_name,
        "as_of": document.as_of,
        "language": document.language,
        "generated_from": document.generated_from,
        "metrics_sha256": document.metrics_sha256,
        "charts": [_entry_payload(entry) for entry in document.charts],
    }


def _parser() -> argparse.ArgumentParser:
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
    """Run the local-only chart CLI."""
    args = _parser().parse_args(argv)
    setup_logging(args.verbose)
    spec = load_and_validate_spec(args.spec)
    out_dir = Path(args.out)
    metrics_path = out_dir / "metrics.json"
    manifest_path = out_dir / "charts.json"
    try:
        grouped = analyze_trends.load_series_csv(out_dir / "series.csv", spec)
        metrics, metrics_sha256 = load_metrics(metrics_path)
        document = build_chart_plan(spec, metrics, metrics_sha256, grouped, str(metrics_path))
        try:
            for entry in document.charts:
                render_chart(entry, out_dir)
            dump_json(_manifest(document), manifest_path)
        except OSError as error:
            raise ChartError(f"could not write charts output: {manifest_path}") from error
    except (ChartError, analyze_trends.AnalysisError) as error:
        print(f"charts failed: {error}", file=sys.stderr)
        return 1
    print(f"Rendered {len(document.charts)} charts; output: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
