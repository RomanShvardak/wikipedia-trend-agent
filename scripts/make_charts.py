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
import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
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
# D-15: the log regime is a CLI opt-in, never a threshold. RESEARCH Pitfall 3
# measured an *automatic* log axis whose visible range began at 3.7 with both
# zero days silently gone and no warning emitted - a chart that deletes exactly
# the observations D-11 protects, on data the reader never chose to compare that
# way. The regime is therefore a named flag, an explicit `nonpositive="mask"`
# argument, and a counted disclosure.
LOG_YSCALE = "log"
LINEAR_YSCALE = "linear"
# The floor of a log axis when EVERY plotted value is non-positive, so there is
# no smallest positive reading to show. A DISPLAY bound, never a data claim.
LOG_FALLBACK_LOWER_BOUND = 1.0
# The disclosure D-15 requires on both the image and in the manifest. A constant
# format string rather than a localized token: this text is the interface a
# reader (and Phase 6) reads to learn what the axis did not draw, and a count
# wrapped in an ambiguous language is exactly the ambiguity it exists to remove.
LOG_NOTE_TEMPLATE = "log scale; {masked} non-positive day(s) masked and not drawn"
# Where the disclosure sits and how it reads, matching the other in-plot text
# tokens so a chart carries one visual language. Font size and colour are
# display-only; no compared number passes through them.
LOG_TEXT_FONT_SIZE = 8
LOG_TEXT_COLOR = "#495057"
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
# D-08: the anomaly marker's geometry. Both endpoints are the anomalies[] entry's
# own `median` and `value`; nothing here re-derives, rescales or narrows them.
# RESEARCH Pattern 4 recorded that a real capture produced
# `{"date": "2025-09-21", "value": 2586, "median": 2189.5}` - a float median,
# because `statistics.median` returns a float on an even-length local window.
ANOMALY_COLOR = "#E8590C"
ANOMALY_LINE_WIDTH = 1.6
ANOMALY_ZORDER = 3
# A `value == median` entry is a zero-length segment, which draws nothing at all.
# A claimed anomaly that renders as an absence is a lie by omission, so the
# equality additionally gets a marker point.
ANOMALY_MARKER = "o"
ANOMALY_MARKER_SIZE = 4.0
# The same idea for a D-15 log entry whose anomaly endpoint the axis cannot
# express: a downward caret at the highest drawable value, so the picture says
# "below the visible range" instead of drawing a stub clipped at the floor.
ANOMALY_MASKED_MARKER = "v"
# One token, used both as the legend entry and inside the subtitle, so a reader -
# and a test - can look for the same word in both places.
ANOMALY_LEGEND_LABEL = "anomaly"
ANOMALY_METHOD_PHRASE = "anomaly markers"
# matplotlib's documented way to keep a handle out of the legend without building
# a proxy artist: the extra series after the first carries this label instead.
NO_LEGEND_LABEL = "_nolegend_"
# D-12/D-13: a gapped period is a grey band plus the text a reader needs to tell
# an absence from the end of the history. RESEARCH verified a labelled `axvspan`
# produces a real legend entry.
GAP_BAND_COLOR = "#ADB5BD"
GAP_BAND_ALPHA = 0.30
GAP_BAND_LABEL = "no data"
GAP_TEXT_COLOR = "#495057"
GAP_TEXT_FONT_SIZE = 7
# A band whose right edge lies within this fraction of the plot width anchors its
# text inward from the band's own end, so a hole at the very start or the very
# end of a history cannot push its own label off the canvas.
GAP_LABEL_EDGE_FRACTION = 0.25
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
NULL_BAR_EDGE = "#6C757D"
# D-04 wants a *visible* hatched bar, and a zero-width rectangle draws nothing:
# the first render of this chart left the 2Y row completely blank, hatch included.
# The n/a stub is therefore this fraction of the already-planned value-axis span -
# a display constant read off `entry.y_limits`, never a data value, and it sits
# beside an "n/a" label so it can never be read as a small measurement.
NULL_BAR_WIDTH_FRACTION = 0.02
# Where the n/a text starts, as a fraction of the axes width. Anchored clear of
# the stub itself, which occupies the first NULL_BAR_WIDTH_FRACTION of the span.
NULL_LABEL_AXES_FRACTION = 0.04
# The contract reason is quoted verbatim (D-04), so it is wrapped to this width
# rather than truncated: a clipped reason is the same defect as a missing one.
NULL_BAR_REASON_WRAP = 34
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


def gap_annotation_text(start: date, end: date) -> str:
    """D-13: the exact string a reader sees under a gapped period.

    Named as a function so the format is asserted against one literal rather
    than pattern-matched at the call site - the text is the reader's only
    evidence that a break is an absence and not the end of the history.
    """
    return f"no data {start.isoformat()}..{end.isoformat()}"


@dataclass(frozen=True, slots=True)
class DataGap:
    """One absent calendar-day range, tagged with the series that lacks those days.

    D-12: a gap exists only where a calendar day is absent from series.csv
    entirely. The tag is what lets the comparison view say WHICH series is
    missing days - an untagged band on a two-line overlay would be an
    unattributed absence, which is the misreading D-13 exists to prevent.
    """

    series_id: str
    start: date
    end: date

    @property
    def days(self) -> int:
        """The inclusive day count, `end - start + 1`."""
        return (self.end - self.start).days + 1

    @property
    def label(self) -> str:
        """The band's own annotation, so plan and image cannot disagree."""
        return gap_annotation_text(self.start, self.end)


def calendar_gaps(points: Sequence[SeriesPoint]) -> tuple[tuple[date, date], ...]:
    """Every range of calendar days absent from one series' own rows (D-12).

    Scoped strictly to the series handed in, and RESEARCH Pitfall 4 is why that
    is load-bearing rather than incidental. `series.csv` is sorted by
    `(series_id, date)`, so the last date of one series is immediately followed
    by the first date of the next: walking the raw file order reports a 365-day
    "gap" between two completely healthy series, and a year-long grey band is
    painted across a chart that has no absence at all.

    Each emitted pair is `(first missing day, last missing day)` - the absent
    days themselves, not the two present days bracketing them. A truncated
    rolling-median window at a series edge is NOT a gap: the underlying days are
    present, only the local window is short.

    The result is sorted and de-duplicated, so two callers cannot disagree about
    ordering and a repeated range cannot be counted twice.
    """
    ordered = sorted(point.date for point in points)
    found: set[tuple[date, date]] = set()
    for previous, current in zip(ordered, ordered[1:]):
        if current - previous == timedelta(days=1):
            continue
        found.add((previous + timedelta(days=1), current - timedelta(days=1)))
    return tuple(sorted(found))


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
    # The line's own absent-day ranges, scoped to this series (D-12). Render-only
    # like every field here; the view publishes the tagged union, never a second
    # copy of the per-line geometry.
    gaps: tuple[DataGap, ...]
    # The line's own anomalies, verbatim from its metrics series. Render-only,
    # like every field here: on the comparison view a marker belongs to a line,
    # so each line carries its own rather than the view carrying one flat pool.
    anomalies: tuple[dict[str, object], ...]
    # The line's own reported calendar range; the view's axis is the union of
    # these, so an absence never compresses the scale (D-10).
    x_limits: tuple[date, date]


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
    gaps: tuple[DataGap, ...]
    anomalies: tuple[dict[str, object], ...]
    anomalies_drawn: int
    # D-15: the scale regime this entry is drawn in. "linear" for every entry
    # unless the caller passed --log-scale, and the growth chart is linear in
    # either case - a percentage axis has no meaningful logarithmic form.
    yscale: str
    # D-15: how many of this entry's own raw readings are non-positive, i.e. how
    # many days this axis could not draw. Always emitted; 0 on every linear
    # entry. The renderer prints the same number it records here, so the picture
    # and the manifest cannot disagree about what the chart left out.
    log_masked_points: int
    # D-15: the visible disclosure, `None` on every linear entry. Omitted from
    # the manifest rather than nulled (the charts.v1 no-null rule).
    log_note: str | None
    y_limits: tuple[float, float]
    # D-10: the date-axis domain, read from the series' own metrics `period`
    # block, on the two kinds that have a date axis. `None` on the growth chart,
    # whose x axis is a percentage axis - and never serialized either way.
    x_limits: tuple[date, date] | None
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


def _subtitle(label: str, anomalies_drawn: int = 0) -> str:
    """D-09: the chart names its own method, next to the spec-authored label.

    `anomalies_drawn` decides whether the anomaly token appears: a chart that
    draws no marker must not claim one, and a chart that draws them must say so.
    """
    phrase = METHOD_PHRASE
    if anomalies_drawn:
        phrase = f"{METHOD_PHRASE} / {ANOMALY_METHOD_PHRASE}"
    return f"{label} - {phrase}"


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


def _iso_date_or_none(value: Any) -> date | None:
    """Parse a contract `YYYY-MM-DD` string, or return None rather than raising."""
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _period_bounds(node: Mapping[str, Any], points: Sequence[SeriesPoint]) -> tuple[date, date]:
    """D-10: the date-axis domain - the series' reported period, not its extremes.

    "Zero-fill" means the axis has no hole, so the domain is the full calendar
    range even across an absence; the line itself still stops where the data
    stops. `period` is the contract's own statement of that range, so reading it
    is a copy rather than a re-derivation; the first and last plotted points are
    the fallback, and they can only ever be narrower than the truth, never wider.
    """
    period = node.get("period")
    if isinstance(period, Mapping):
        start = _iso_date_or_none(period.get("start"))
        end = _iso_date_or_none(period.get("end"))
        if start is not None and end is not None and end >= start:
            return (start, end)
    return (points[0].date, points[-1].date)


def _anomaly_list(
    node: Mapping[str, Any], points: Sequence[SeriesPoint], series_id: str
) -> tuple[dict[str, object], ...]:
    """Copy metrics.json's `anomalies[]` onto a chart verbatim, after four guards.

    D-08: the drawn marker's two endpoints are this entry's own `median` and its
    own `value`, so nothing here transforms them - and nothing downstream may
    either. The chart layer therefore introduces no statistic: no re-estimated
    z-score, no recomputed MAD, no narrowed median.

    The four refusals each block a claim the picture could not otherwise support:
    - a missing or non-object entry, so a half-read anomaly is never drawn as a
      partial marker;
    - a date that is not literally `YYYY-MM-DD`. Python 3.11's
      `date.fromisoformat` also accepts the basic "20250512" form, so the shape
      cannot be delegated to it - this mirrors `analyze_trends._parse_iso_date`.
    - a `value`/`median` that is not a number, so the axis cannot be handed
      something it would silently coerce.
    - a date the series has no row for (T-5-17): a marker over a day the chart
      does not draw is a claim the picture cannot back.

    `value` and `median` are kept as `int | float` exactly as read. `median` is
    typed that way in 05-02 and stays that way here: `statistics.median` returns
    a float for an even-length window, and narrowing one would silently move a
    drawn coordinate.
    """
    raw = node.get("anomalies")
    if not isinstance(raw, list):
        raise ChartError(f"anomalies must be a list for series: {series_id}")
    present = {point.date for point in points}
    copied: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ChartError(f"anomaly must be an object for series: {series_id}")
        for field in ("date", "value", "median"):
            if field not in item:
                raise ChartError(f"anomaly is missing {field!r} for series: {series_id}")
        text = item["date"]
        if not isinstance(text, str) or len(text) != 10 or text[4] != "-" or text[7] != "-":
            raise ChartError(f"anomaly date is not YYYY-MM-DD: {text!r}")
        try:
            when = date.fromisoformat(text)
        except ValueError as error:
            raise ChartError(f"anomaly date is not YYYY-MM-DD: {text!r}") from error
        for field in ("value", "median"):
            number = item[field]
            if isinstance(number, bool) or not isinstance(number, (int, float)):
                raise ChartError(
                    f"anomaly {field} must be a number for series: {series_id}"
                )
        if when not in present:
            raise ChartError(f"anomaly date is not present in series.csv: {text}")
        # A shallow copy, so the plan cannot be mutated through the caller's
        # document while the rendered numbers are already decided.
        copied.append(dict(item))
    return tuple(copied)


def _nonpositive_count(values: Sequence[int | float]) -> int:
    """How many of these readings a log axis cannot draw (D-15).

    A logarithmic scale has no place for zero or a negative number, so on a log
    chart every such reading is masked. The count is the disclosure's only
    substance: a reader who is not told how many days the axis dropped cannot
    tell a quiet series from a selectively drawn one.
    """
    return sum(1 for value in values if value <= 0)


def _log_lower_bound(values: Sequence[int | float]) -> float:
    """The display floor of a log axis: the smallest strictly positive reading.

    A display bound and never a data claim - which is why the all-non-positive
    case falls back to a literal 1.0 rather than to a computed value: with
    nothing positive to show, any number here is arbitrary, and an arbitrary
    number must be recognisable as one.
    """
    positives = [float(value) for value in values if value > 0]
    if not positives:
        return LOG_FALLBACK_LOWER_BOUND
    return min(positives)


def _log_note(masked: int) -> str:
    """The exact disclosure string a log chart prints and charts.json publishes."""
    return LOG_NOTE_TEMPLATE.format(masked=masked)


def _timeseries_entry(
    node: Mapping[str, Any],
    observations: Sequence[analyze_trends.Observation],
    spec_index: int,
    *,
    log_scale: bool = False,
) -> ChartEntry:
    """Build the one timeseries entry a series owns; plan 05-04 adds the growth sibling."""
    series_id = _require_str(node, "series_id", "metrics.series entry")
    label = _require_str(node, "label", "metrics.series entry")
    points = tuple(SeriesPoint(observation.date, observation.views) for observation in observations)
    raw_values = tuple(point.views for point in points)
    anomalies = _anomaly_list(node, points, series_id)
    # D-12: the gap walk runs on THIS series' own points, never on the
    # concatenated file order (RESEARCH Pitfall 4).
    gaps = tuple(
        DataGap(series_id, start, end) for start, end in calendar_gaps(points)
    )
    masked = _nonpositive_count(raw_values) if log_scale else 0
    ceiling = float(max(raw_values)) * Y_HEADROOM_FACTOR
    # D-14: the floor is the literal 0.0, never a computed value, so no axis can
    # ever imply negative views. D-15 changes the REGIME, not that rule: a log
    # axis cannot express a zero floor at all, which is why it is a separately
    # labelled mode with its own disclosure rather than a variant of this one.
    floor = _log_lower_bound(raw_values) if log_scale else 0.0
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
        gaps=gaps,
        anomalies=anomalies,
        # The single definition of this field, for every entry of every kind: it
        # counts the list it travels with. The growth entry below carries an
        # empty list for the same reason it draws no marker, so the identity
        # holds there too.
        anomalies_drawn=len(anomalies),
        yscale=LOG_YSCALE if log_scale else LINEAR_YSCALE,
        log_masked_points=masked,
        log_note=_log_note(masked) if log_scale else None,
        y_limits=(floor, ceiling),
        bars=(),
        note=None,
        subtitle=_subtitle(label, len(anomalies)),
        x_limits=_period_bounds(node, points),
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
        # D-08 is a timeseries/overlay rule. A growth chart summarises windows, so
        # a daily spike drawn there would be a category error; leaving the list
        # empty (rather than filtering it at the manifest) is what keeps
        # `anomalies_drawn == len(anomalies)` true for this kind as well.
        anomalies=(),
        anomalies_drawn=0,
        # D-15: the flag reaches the timeseries and overlay axes only. The growth
        # branch does not even read `yscale` - `test_growth_stays_linear_under_log_scale`
        # asserts that structurally - so a percentage bar chart is never a log axis.
        yscale=LINEAR_YSCALE,
        log_masked_points=0,
        log_note=None,
        y_limits=_growth_value_limits(bars),
        # No date axis on this chart, so there is no D-10 domain to publish.
        x_limits=None,
        bars=bars,
        note=None,
        subtitle=f"{label} - {GROWTH_METHOD_PHRASE}",
    )


def _overlay_entry(
    per_series: Sequence[ChartEntry], spec_language: str, *, log_scale: bool = False
) -> ChartEntry:
    """Build the one comparison view a document publishes, last in charts[].

    D-20: one shared *raw* y-axis, never a normalized or rebased one. Each line
    reuses its series' own already-built values, so the comparison view cannot
    introduce a number with no source in metrics.json. The same reasoning gives
    the comparison view the union of its lines' anomalies: an anomaly belongs to
    a series, and the view's own count is the length of the list it holds.
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
            gaps=entry.gaps,
            anomalies=entry.anomalies,
            x_limits=entry.x_limits or (entry.points[0].date, entry.points[-1].date),
        )
        for entry in per_series
    )
    anomalies = tuple(anomaly for line in lines for anomaly in line.anomalies)
    # D-10 on the comparison view: the domain is the union of the lines' own
    # reported ranges, so one series' absence never compresses another's scale.
    gaps = tuple(gap for line in lines for gap in line.gaps)
    start = min(line.x_limits[0] for line in lines)
    end = max(line.x_limits[1] for line in lines)
    # D-14 applied to the overlay as a timeseries axes - which is what it is.
    # One shared ceiling over every series, so the axis is comparable.
    largest = max(max(line.raw_values) for line in lines)
    # D-15 on the comparison view: one shared regime, one shared disclosure, and
    # the masked count is the union over every line - a line's non-positive days
    # are absent from the SHARED axis, not just from its own plot, so counting
    # them per line would under-report what this chart did not draw.
    all_values = [value for line in lines for value in line.raw_values]
    masked = _nonpositive_count(all_values) if log_scale else 0
    floor = _log_lower_bound(all_values) if log_scale else 0.0
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
        # The tagged union of the lines' own absences, so a reader of the
        # comparison view is never left guessing which series is missing days.
        gaps=gaps,
        anomalies=anomalies,
        anomalies_drawn=len(anomalies),
        yscale=LOG_YSCALE if log_scale else LINEAR_YSCALE,
        log_masked_points=masked,
        log_note=_log_note(masked) if log_scale else None,
        y_limits=(floor, float(largest) * Y_HEADROOM_FACTOR),
        bars=(),
        note=OVERLAY_NOTE,
        subtitle=f"{OVERLAY_LABEL} - {OVERLAY_METHOD_PHRASE}"
        + (f" / {ANOMALY_METHOD_PHRASE}" if anomalies else ""),
        x_limits=(start, end),
    )


def build_chart_plan(
    spec: Mapping[str, Any],
    metrics: Mapping[str, Any],
    metrics_sha256: str,
    grouped: Mapping[str, Sequence[analyze_trends.Observation]],
    metrics_path: str,
    *,
    log_scale: bool = False,
) -> ChartDocument:
    """Decide every number that will be drawn, without importing a backend.

    Growth, totals, averages, and anomaly local medians are read from
    `metrics.json` and never recomputed (ANAL-06 / CHRT-02). The single
    permitted derivation is the centered rolling median, and it never feeds a
    numeric label.

    `log_scale` is keyword-only and defaults to False because D-15 makes the log
    regime an explicit opt-in: a caller that does not name the flag gets the
    linear axis, and no threshold anywhere in this module can change that.
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
        charts.append(
            _timeseries_entry(
                node, observations, spec_index_by_id[series_id], log_scale=log_scale
            )
        )
        charts.append(_growth_entry(node, spec_index_by_id[series_id]))
    # D-01/D-19: the comparison view is a *separate* chart, appended last. It
    # never replaces or suppresses the per-series charts, because a per-series
    # view and a comparison answer different questions. It is fed the
    # *timeseries* entries only: a growth entry carries no daily series, so
    # including it would contribute an empty line to the shared axis.
    charts.append(
        _overlay_entry(
            [c for c in charts if c.kind == TIMESERIES_KIND], language, log_scale=log_scale
        )
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


def _day_sequence(start: date, end: date) -> list[date]:
    """Every calendar day from `start` to `end`, inclusive."""
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _plotted_coordinates(
    points: Sequence[SeriesPoint],
    values: Sequence[int | float],
    gaps: Sequence[DataGap],
    mdates: Any,
) -> tuple[list[float], list[int | float]]:
    """The x positions and y values to draw, with a NaN standing in for each absence.

    D-12/D-13: a NaN is the only permitted expression of a missing calendar day.
    matplotlib breaks a line at a NaN with no interpolation, so the reader sees
    the break; a bridging implementation would draw a straight line across the
    hole, which is the same fabrication `null != 0` exists to prevent. The NaNs
    are render-only - they are never written to the manifest, never labelled and
    never counted as observations.

    `values` are the series' own real values, copied element for element. A
    `views=0` row passes through untouched (D-11): a zero is a reading, and
    only an ABSENT day is a NaN here.
    """
    x_numbers: list[float] = []
    y_values: list[int | float] = []
    gap_index = 0
    for point, value in zip(points, values):
        # Emit every absence that closes before this point, in order. A point can
        # never fall inside its own series' gap - those days are absent by
        # definition - so this is the only place a NaN belongs.
        while gap_index < len(gaps) and gaps[gap_index].end < point.date:
            for day in _day_sequence(gaps[gap_index].start, gaps[gap_index].end):
                x_numbers.append(mdates.date2num(day))
                y_values.append(float("nan"))
            gap_index += 1
        x_numbers.append(mdates.date2num(point.date))
        y_values.append(value)
    return x_numbers, y_values


def _gap_label_anchor(
    start_number: float, end_number: float, domain_start: float, domain_span: float
) -> tuple[float, str]:
    """Where the band's text sits, and how it hangs off that point.

    A band near either edge of the plot anchors INWARD on the band itself; a band
    in the middle is centred on it. This is 05-04's clipping failure turned into
    a rule: the first render of this annotation ran "no data 2024-09-24..2024-10-09"
    off the left edge of the image and "no data 2026-09-04..2026-09-19" off the
    right, because both were centred on a band that sat almost at the axis end.
    No structural assertion could see it - only opening the PNG did.
    """
    end_fraction = (end_number - domain_start) / domain_span
    if end_fraction < GAP_LABEL_EDGE_FRACTION:
        return end_number, "left"
    if end_fraction > 1.0 - GAP_LABEL_EDGE_FRACTION:
        return start_number, "right"
    return (start_number + end_number) / 2.0, "center"


def _draw_gap_bands(
    ax: Any, gaps: Sequence[DataGap], mdates: Any, domain: tuple[date, date]
) -> None:
    """D-13: a labelled grey band plus its date range, once per distinct absence.

    The band is what stops a break being read as the end of the history, and the
    text is what says which days are missing. `axvspan` is de-duplicated by
    range: when two series are absent over the same days the view draws one band
    at one alpha, and the manifest keeps both tagged records so the attribution
    survives. Only the first band is labelled - two identical "no data" legend
    rows on a chart with two absences is noise, and the count of absences is
    already in the manifest.
    """
    domain_start = mdates.date2num(domain[0])
    domain_span = mdates.date2num(domain[1]) - domain_start
    drawn: set[tuple[date, date]] = set()
    labelled = False
    for gap in gaps:
        if (gap.start, gap.end) in drawn:
            continue
        drawn.add((gap.start, gap.end))
        start_number = mdates.date2num(gap.start)
        end_number = mdates.date2num(gap.end)
        ax.axvspan(
            start_number,
            end_number,
            color=GAP_BAND_COLOR,
            alpha=GAP_BAND_ALPHA,
            label=GAP_BAND_LABEL if not labelled else NO_LEGEND_LABEL,
        )
        labelled = True
        anchor, alignment = _gap_label_anchor(start_number, end_number, domain_start, domain_span)
        # The y anchor is an axes fraction and the alignment flips with the
        # band's position, so the text stays inside the plot wherever the band
        # falls - the clipping failure 05-04 hit with the n/a reason.
        ax.annotate(
            gap.label,
            xy=(anchor, 0.0),
            xycoords=("data", "axes fraction"),
            xytext=(0, 4),
            textcoords="offset points",
            ha=alignment,
            va="bottom",
            fontsize=GAP_TEXT_FONT_SIZE,
            color=GAP_TEXT_COLOR,
        )


def _draw_anomaly_marks(
    ax: Any,
    anomalies: Sequence[Mapping[str, Any]],
    labelled: bool,
    masked_anchor: float | None = None,
) -> None:
    """D-08: one vertical segment per anomaly, between the contract's own numbers.

    The segment's two ends are `anomaly["median"]` and `anomaly["value"]` - read
    by subscript, exactly as metrics.json wrote them, and coerced to nothing. A
    re-estimated z-score here would let the picture disagree with the document
    the report is built from, which is the failure this phase exists to prevent.

    `labelled` is True for the first marker on a chart and False for every one
    after it, so a chart with three anomalies names them once in the legend
    rather than three times. `anomalies: []` draws nothing and claims nothing.

    `masked_anchor` is not None only on a log entry, and carries that entry's own
    axis floor. It is what D-11 and D-15 collide on: a `views=0` day is data the
    detector legitimately reports as an anomaly, and a logarithmic axis has no
    place to put it. Drawing the segment anyway is worse than omitting it - the
    axes clip it at the floor, and the resulting stub is indistinguishable from a
    genuine reading that landed exactly on the floor. So a masked endpoint is
    drawn as a downward caret AT the highest drawable endpoint instead, which
    says "this day is below the visible range", and the log note says how many
    days that is. The same anomaly colour and the same legend row are reused, so
    a masked marker is still recognisably an anomaly marker and
    `anomalies_drawn == len(anomalies)` keeps holding for every kind.
    """
    for index, anomaly in enumerate(anomalies):
        when = date.fromisoformat(anomaly["date"])
        label = ANOMALY_LEGEND_LABEL if (labelled and index == 0) else NO_LEGEND_LABEL
        if masked_anchor is not None and (
            anomaly["value"] <= 0 or anomaly["median"] <= 0
        ):
            # The highest endpoint the axis can actually show. With one masked
            # endpoint this is the median, and with two it is the floor itself -
            # either way it is a position the reader can see, carrying a marker
            # whose direction states the real value is below it.
            anchor = max(float(anomaly["median"]), float(masked_anchor))
            ax.plot(
                when,
                anchor,
                marker=ANOMALY_MASKED_MARKER,
                ms=ANOMALY_MARKER_SIZE,
                color=ANOMALY_COLOR,
                zorder=ANOMALY_ZORDER,
                label=label,
            )
            continue
        ax.vlines(
            when,
            anomaly["median"],
            anomaly["value"],
            color=ANOMALY_COLOR,
            lw=ANOMALY_LINE_WIDTH,
            zorder=ANOMALY_ZORDER,
            label=label,
        )
        # A zero-length segment draws nothing. The detector cannot emit one at
        # MAD_K = 3.5, but a hand-built document can, and a claimed anomaly that
        # renders as an absence is a lie by omission - so the marker point rides
        # along on the equality rather than replacing the segment.
        if anomaly["value"] == anomaly["median"]:
            ax.plot(
                when,
                anomaly["value"],
                marker=ANOMALY_MARKER,
                ms=ANOMALY_MARKER_SIZE,
                color=ANOMALY_COLOR,
                zorder=ANOMALY_ZORDER,
                label=NO_LEGEND_LABEL,
            )


def _apply_log_regime(ax: Any, entry: ChartEntry) -> None:
    """D-15: put the entry's own scale regime on the axes, explicitly.

    Two properties are load-bearing and neither is left to a default:

    1. The regime comes from `entry.yscale`, so the only thing that can select
       it is the `--log-scale` flag. No ratio, threshold or data-dependent test
       reaches this function - that is what keeps RESEARCH Pitfall 3's automatic
       switch, which deleted zero days without telling anyone, unreachable.
    2. `nonpositive="mask"` is written out. matplotlib's default happens to be
       `mask` today; a future `clip` would drop the same days in silence, and
       nothing else in the suite could see the difference.
    """
    if entry.yscale != LOG_YSCALE:
        return
    ax.set_yscale("log", nonpositive="mask")


def _draw_log_disclosure(ax: Any, entry: ChartEntry) -> None:
    """Draw D-15's counted disclosure into the image, not only into the manifest.

    A PNG that hid masked days without saying so is precisely the silent
    distortion this phase exists to prevent, and a PNG travels: it can be
    separated from its report and quoted on its own. The text sits at the
    top-RIGHT in axes fractions because the top-left is already occupied - by
    the legend on the timeseries and by the scales-differ note on the overlay -
    and 05-04/05-05 both shipped clipped annotations from a bad anchor.
    `test_log_disclosure_text_stays_inside_the_canvas` measures the result
    against the figure, so the anchor is verified rather than trusted.
    """
    if not entry.log_note:
        return
    ax.annotate(
        entry.log_note,
        xy=(1.0, 1.0),
        xycoords="axes fraction",
        xytext=(-4, -8),
        textcoords="offset points",
        ha="right",
        va="top",
        fontsize=LOG_TEXT_FONT_SIZE,
        color=LOG_TEXT_COLOR,
    )


def _draw_timeseries(entry: ChartEntry, ax: Any, mdates: Any) -> None:
    """Draw one series: a thin raw daily line under a bold 7-day median line.

    D-07 keeps the raw line visible so an anomaly marker lands on a line the
    reader can see. D-14's zero floor is a timeseries rule and is applied here,
    off the entry's own limits - and D-15's log regime replaces that floor with
    the smallest positive reading, which is the only honest floor a
    logarithmic axis can have.
    """
    _apply_log_regime(ax, entry)
    x_values, raw_values = _plotted_coordinates(
        entry.points, entry.raw_values, entry.gaps, mdates
    )
    _, median_values = _plotted_coordinates(
        entry.points, entry.median_values, entry.gaps, mdates
    )
    ax.plot(x_values, raw_values, lw=0.6, alpha=0.55, color="#4C6EF5", label=RAW_LINE_LABEL)
    ax.plot(
        x_values, median_values, lw=1.8, color="#212529", label=MEDIAN_LINE_LABEL
    )
    _draw_gap_bands(ax, entry.gaps, mdates, entry.x_limits or (
        entry.points[0].date, entry.points[-1].date
    ))
    _draw_anomaly_marks(
        ax,
        entry.anomalies,
        labelled=True,
        masked_anchor=entry.y_limits[0] if entry.yscale == LOG_YSCALE else None,
    )
    # D-14: an explicit zero floor, timeseries axes only. A growth chart must
    # never receive this call - it would erase a negative bar (RESEARCH
    # Pitfall 1). On a log entry the plan's floor is the smallest positive
    # reading rather than 0.0, because a logarithmic axis has no place for zero.
    ax.set_ylim(entry.y_limits[0], entry.y_limits[1])
    # D-10: the axis spans the full calendar range the series reports, so the
    # scale itself has no hole; the absence is expressed as a break in the line
    # plus a band, never as a compressed axis. Only rows present in series.csv
    # are plotted, and a views=0 row is drawn at face value (D-11).
    assert entry.x_limits is not None, "a timeseries chart always has a date axis"
    ax.set_xlim(mdates.date2num(entry.x_limits[0]), mdates.date2num(entry.x_limits[1]))
    ax.xaxis_date()
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    _draw_log_disclosure(ax, entry)
    ax.legend(loc="upper left", frameon=False, fontsize=8)


def _draw_overlay(entry: ChartEntry, ax: Any, mdates: Any) -> None:
    """Draw every series on one shared raw y-axis, plus the scales-differ note.

    D-20 forbids a *normalized or rebased* axis here: that would invent a
    per-series index with no source in metrics.json. It does not forbid D-15's
    explicitly-labelled log regime, which rescales nothing - it only changes how
    the reader's eye is spaced along the ONE shared axis, and it comes with the
    counted disclosure below. The x-axis spans the union of the lines' own
    reported calendar ranges (D-10) - bounds read off the plan, never invented
    by matplotlib - and the y-axis is a zero-anchored timeseries axis (D-14),
    or the smallest positive reading on a log entry.
    """
    _apply_log_regime(ax, entry)
    for index, line in enumerate(entry.series_lines):
        x_values, raw_values = _plotted_coordinates(
            line.points, line.raw_values, line.gaps, mdates
        )
        ax.plot(
            x_values,
            raw_values,
            lw=0.9,
            alpha=0.8,
            color=OVERLAY_COLOR_CYCLE[index % len(OVERLAY_COLOR_CYCLE)],
            label=line.label,
        )
    _draw_gap_bands(
        ax, entry.gaps, mdates, entry.x_limits or (entry.points[0].date, entry.points[-1].date)
    )
    # Each line draws its own markers, so an anomaly stays attached to the series
    # it belongs to; the legend names them once for the whole chart.
    drew_any = False
    masked_anchor = entry.y_limits[0] if entry.yscale == LOG_YSCALE else None
    for line in entry.series_lines:
        _draw_anomaly_marks(
            ax, line.anomalies, labelled=not drew_any, masked_anchor=masked_anchor
        )
        drew_any = drew_any or bool(line.anomalies)
    ax.set_ylim(entry.y_limits[0], entry.y_limits[1])
    # D-10: the union of the lines' reported ranges, so the scale has no hole
    # even where a series is absent.
    assert entry.x_limits is not None, "the comparison view always has a date axis"
    ax.set_xlim(mdates.date2num(entry.x_limits[0]), mdates.date2num(entry.x_limits[1]))
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
    _draw_log_disclosure(ax, entry)
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
    # The only arithmetic in this body: the width of the n/a stub, as a fraction
    # of the span the plan already computed. It is a display constant, never a
    # measurement - no bar value passes through it.
    span = entry.y_limits[1] - entry.y_limits[0]
    stub_width = span * NULL_BAR_WIDTH_FRACTION
    # A label anchored at a bar's end and extending outward runs off the plot
    # when that end sits near the axis edge - the first render clipped the 3M
    # label against the right spine. Anchoring each label *inward* (toward the
    # axis midpoint) keeps it inside the axes for every bar position, and the
    # label is drawn above its own bar, never on it.
    midpoint = (entry.y_limits[0] + entry.y_limits[1]) / 2.0
    for index, bar in enumerate(entry.bars):
        window_label = WINDOW_LABELS[bar.window]
        labels.append(window_label)
        if bar.pct is None:
            # D-04: visible, hatched, grey, and annotated with the contract
            # reason. Never a missing row, never a zero-height solid bar. The
            # annotation is anchored in axes fractions, not data coordinates,
            # so the reason stays inside the plot on any axis sign.
            drawn = ax.barh(index, stub_width, height=BAR_HEIGHT)
            for patch in drawn:
                patch.set_color(NULL_BAR_COLOR)
                patch.set_edgecolor(NULL_BAR_EDGE)
                patch.set_hatch(NULL_BAR_HATCH)
            ax.annotate(
                f"{REASON_NA_LABEL}\n{textwrap.fill(bar.reason or '', NULL_BAR_REASON_WRAP)}",
                xy=(NULL_LABEL_AXES_FRACTION, index),
                xycoords=("axes fraction", "data"),
                xytext=(0, 0),
                textcoords="offset points",
                va="center",
                ha="left",
                fontsize=7,
                color="#495057",
            )
            continue
        drawn = ax.barh(index, bar.pct, height=BAR_HEIGHT)
        for patch in drawn:
            patch.set_color(GROWTH_BAR_COLOR)
        # D-05: value, window and volume base on the chart itself, so every
        # dynamic number is traceable without the report. The label sits ABOVE
        # its own bar's end, not beside it: beside it, a left-anchored label on a
        # negative bar lands on the y tick labels, which is what the first render
        # showed. The base is formatted with a space thousands separator
        # (display-only; the compared value in charts.json is unformatted).
        ax.annotate(
            f"{window_label} {bar.pct:+.1f}%\non {bar.base_avg_daily_views: .1f} views/day",
            xy=(bar.pct, index),
            xytext=(0, 9),
            textcoords="offset points",
            va="bottom",
            ha="right" if bar.pct >= midpoint else "left",
            fontsize=7,
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


def _gap_payload(gap: DataGap) -> dict[str, object]:
    """Serialize one absent-day range under the charts.v1 no-null rule.

    `series_id` is what makes the record attributable: on the comparison view the
    same range can be absent for one series and present for the other, and an
    untagged band would claim an absence nobody had. `days` is `end - start + 1`,
    the inclusive count a reader would count by hand. No field here is ever
    nulled - an absence is a fact with four parts, not a missing key.
    """
    return {
        "series_id": gap.series_id,
        "start": gap.start.isoformat(),
        "end": gap.end.isoformat(),
        "days": gap.days,
    }


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
        # D-15: always emitted, 0 on a linear entry, so a consumer reads the
        # masked count off the same field in both regimes rather than inferring
        # it from a missing key. `log_note` follows the omission rule instead -
        # there is nothing to say on a linear chart, so the key is absent.
        "log_masked_points": entry.log_masked_points,
        "y_limits": [entry.y_limits[0], entry.y_limits[1]],
        "points": _plotted_point_count(entry),
        "gaps": [_gap_payload(gap) for gap in entry.gaps],
        "anomalies_drawn": entry.anomalies_drawn,
        "subtitle": entry.subtitle,
    }
    if entry.log_note is not None:
        payload["log_note"] = entry.log_note
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
        "--log-scale",
        action="store_true",
        help=(
            "opt-in labelled log y-axis for the timeseries and comparison-view charts only; "
            "growth bars stay linear, and non-positive days are masked and counted on the "
            "chart and in charts.json"
        ),
    )
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
        document = build_chart_plan(
            spec, metrics, metrics_sha256, grouped, str(metrics_path), log_scale=args.log_scale
        )
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
