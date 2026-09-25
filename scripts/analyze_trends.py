"""Analyze local Wikipedia pageview observations into frozen metrics.json."""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction
from pathlib import Path
from statistics import median
from typing import Any, cast

import common
from common import dump_json, load_and_validate_spec, setup_logging

CSV_HEADER = ("date", "views", "series_id", "project", "article")
GROWTH_WINDOWS = {"m3": 91, "y1": 365, "y2": 730}
COVERAGE_RATIO = 0.8
MAD_RADIUS_DAYS = 3
MAD_SCALE = 1.4826
MAD_K = 3.5
MIN_SERIES_OBSERVATIONS = 14
MIN_LOCAL_OBSERVATIONS = 3
MIN_PERIOD_DAYS = 91
LONG_PERIOD_DAYS = 730
MIN_MONTHLY_VIEWS = 1000.0
HIGH_MONTHLY_VIEWS = 10000.0
ANOMALY_SHARE_BOUNDARY = 0.05
METHODOLOGY_BREAK = date(2015, 5, 1)
DIRECTION_BAND_PCT = 10.0
MAX_CSV_BYTES = 16 * 1024 * 1024
MAX_OBSERVATIONS = 50_000
MAX_VIEWS_DIGITS = 20

# Joint per-half quadratic trend plus shared calendar-effect model. Two complete
# aligned 365-day halves each carry their own three-term daily trend, and all 12
# calendar months share one zero-sum additive effect enforced through
# s_12 = -(s_1 + ... + s_11). That is 2*3 + 11 = 17 free parameters, solved once.
SEASONALITY_HALVES = 2
TREND_TERMS = 3
FREE_MONTH_EFFECTS = 11
SEASONALITY_PARAMETERS = SEASONALITY_HALVES * TREND_TERMS + FREE_MONTH_EFFECTS
MONTH_COLUMN_0 = SEASONALITY_HALVES * TREND_TERMS
MONTH_STAT_KEYS = ("rows", "x", "x2", "x3", "x4", "y", "xy", "x2y")

INSUFFICIENT_OBSERVATIONS_REASON = "insufficient observations in one or both equal-length windows"
ZERO_PREVIOUS_MEAN_REASON = "previous equal-length window has zero mean"
PERIOD_BELOW_MINIMUM_REASON = "period below 91 days"
PERIOD_MINIMUM_REASON = "period at least 91 days"
PERIOD_LONG_REASON = "period at least 730 days"
VOLUME_BELOW_MINIMUM_REASON = "monthly 30-day views below 1000"
VOLUME_MINIMUM_REASON = "monthly 30-day views at least 1000"
VOLUME_HIGH_REASON = "monthly 30-day views at least 10000"
NO_ANOMALIES_REASON = "no anomalies detected"
LIMITED_ANOMALIES_REASON = "anomaly share within 5 percent"
HIGH_ANOMALIES_REASON = "anomaly share above 5 percent"
MISSING_CLEAN_Y1_REASON = "clean 1-year growth unavailable"
METHODOLOGY_CROSSING_REASON = "comparison crosses 2015-05-01 methodology break"
LOW_CONFIDENCE_HYPOTHESIS_REASON = (
    "low confidence: treat the reading as a hypothesis"
)
SEASONALITY_UNAVAILABLE_NOTE = (
    "fewer than two aligned 365-day halves are available"
)
SEASONALITY_NO_PEAKS_NOTE = "no recurring peak months identified"
SEASONALITY_AVAILABLE_NOTE = (
    "recurring peaks have positive shared month effects and positive pre-season "
    "residual means in both aligned halves; repeatability is measured before "
    "subtracting the fitted shared effect, and month-scale monotonic curvature "
    "outside the fitted linear/quadratic daily trend can still be confounded "
    "with calendar effects across only two observed cycles"
)


@dataclass(frozen=True, slots=True)
class Observation:
    date: date
    views: int | float
    series_id: str
    project: str
    article: str


class AnalysisError(RuntimeError):
    """A model-readable local input or analysis failure."""


def _as_spec_series(spec: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    series = spec.get("series")
    if not isinstance(series, list) or not series:
        raise AnalysisError("spec.series must be a non-empty list")
    by_id: dict[str, Mapping[str, Any]] = {}
    for item in series:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise AnalysisError("spec.series items must contain a string id")
        series_id = item["id"]
        if series_id in by_id:
            raise AnalysisError(f"duplicate spec series id: {series_id}")
        by_id[series_id] = item
    return by_id


def _parse_iso_date(value: str, row_number: int) -> date:
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise AnalysisError(f"row {row_number}: date must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise AnalysisError(f"row {row_number}: date must be YYYY-MM-DD") from error


def _parse_views_cell(views_text: str, row_number: int) -> int:
    if not views_text.isascii() or not views_text.isdigit():
        raise AnalysisError(f"row {row_number}: views must be a non-negative integer")
    if len(views_text) > MAX_VIEWS_DIGITS:
        raise AnalysisError(
            f"row {row_number}: views must contain at most {MAX_VIEWS_DIGITS} digits"
        )
    try:
        return int(views_text)
    except ValueError as error:
        raise AnalysisError(f"row {row_number}: views must be a non-negative integer") from error


def _spec_window_bounds(spec: Mapping[str, Any]) -> tuple[date, date]:
    window = spec.get("window")
    if not isinstance(window, Mapping):
        raise AnalysisError("spec.window must be an object")
    start_text = window.get("start")
    end_text = window.get("end")
    if not isinstance(start_text, str) or not isinstance(end_text, str):
        raise AnalysisError("spec.window must contain string start and end bounds")
    try:
        start = date(
            int(start_text[0:4]), int(start_text[4:6]), int(start_text[6:8])
        )
        end = date(int(end_text[0:4]), int(end_text[4:6]), int(end_text[6:8]))
    except ValueError as error:
        raise AnalysisError("spec.window bounds must be valid YYYYMMDD dates") from error
    if end < start:
        raise AnalysisError("spec.window end must not precede start")
    return start, end


def load_series_csv(
    path: str | Path, spec: Mapping[str, Any]
) -> dict[str, list[Observation]]:
    """Load and strictly join a local normalized CSV to the validated spec."""
    csv_path = Path(path)
    try:
        size = csv_path.stat().st_size
    except OSError as error:
        raise AnalysisError(f"could not read series CSV: {csv_path}") from error
    if size == 0:
        raise AnalysisError("series CSV is empty")
    if size > MAX_CSV_BYTES:
        raise AnalysisError(f"series CSV exceeds {MAX_CSV_BYTES} bytes")

    spec_series = _as_spec_series(spec)
    window_start, window_end = _spec_window_bounds(spec)
    grouped: dict[str, list[Observation]] = {series_id: [] for series_id in spec_series}
    seen: set[tuple[str, date]] = set()
    row_count = 0
    try:
        with csv_path.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle, strict=True)
            try:
                header = next(reader)
            except StopIteration as error:
                raise AnalysisError("series CSV is empty") from error
            if tuple(header) != CSV_HEADER:
                raise AnalysisError(
                    f"series CSV header must be exactly {','.join(CSV_HEADER)}"
                )
            for row_number, row in enumerate(reader, start=2):
                row_count += 1
                if row_count > MAX_OBSERVATIONS:
                    raise AnalysisError(
                        f"series CSV exceeds {MAX_OBSERVATIONS} observations"
                    )
                if len(row) != len(CSV_HEADER):
                    raise AnalysisError(
                        f"row {row_number}: expected {len(CSV_HEADER)} CSV fields"
                    )
                date_text, views_text, series_id, project, article = row
                if series_id not in spec_series:
                    raise AnalysisError(
                        f"row {row_number}: series_id is not present in spec: {series_id}"
                    )
                expected = spec_series[series_id]
                if (
                    project != expected.get("project")
                    or article != expected.get("article")
                ):
                    raise AnalysisError(
                        f"row {row_number}: project/article do not match spec for {series_id}"
                    )
                views = _parse_views_cell(views_text, row_number)
                observed_date = _parse_iso_date(date_text, row_number)
                if not window_start <= observed_date <= window_end:
                    raise AnalysisError(
                        f"row {row_number}: date is outside the inclusive spec window"
                    )
                identity = (series_id, observed_date)
                if identity in seen:
                    raise AnalysisError(
                        f"row {row_number}: duplicate date for series {series_id}"
                    )
                seen.add(identity)
                grouped[series_id].append(
                    Observation(
                        date=observed_date,
                        views=views,
                        series_id=series_id,
                        project=project,
                        article=article,
                    )
                )
    except csv.Error as error:
        raise AnalysisError("series CSV is malformed") from error
    except UnicodeDecodeError as error:
        raise AnalysisError("series CSV must be valid UTF-8") from error
    except OSError as error:
        raise AnalysisError(f"could not read series CSV: {csv_path}") from error

    missing = [series_id for series_id, rows in grouped.items() if not rows]
    if missing:
        raise AnalysisError(f"series CSV has no observations for: {', '.join(missing)}")
    for rows in grouped.values():
        rows.sort(key=lambda observation: observation.date)
    return grouped


def detect_anomalies(observations: Sequence[Observation]) -> list[dict[str, object]]:
    """Detect isolated anomalies with a centered date window and scaled MAD."""
    if len(observations) < MIN_SERIES_OBSERVATIONS:
        return []
    found: list[dict[str, object]] = []
    for observation in observations:
        local = [
            candidate.views
            for candidate in observations
            if abs((candidate.date - observation.date).days) <= MAD_RADIUS_DAYS
        ]
        if len(local) < MIN_LOCAL_OBSERVATIONS:
            continue
        local_median = median(local)
        raw_mad = median(abs(value - local_median) for value in local)
        if raw_mad == 0:
            continue
        score = (observation.views - local_median) / (raw_mad * MAD_SCALE)
        if abs(score) >= MAD_K:
            found.append(
                {
                    "date": observation.date.isoformat(),
                    "value": observation.views,
                    "median": local_median,
                }
            )
    return found


def replace_anomalies(
    observations: Sequence[Observation], anomalies: Sequence[Mapping[str, object]]
) -> list[Observation]:
    """Replace anomalous values with their detected local medians, preserving dates."""
    replacements: dict[str, int | float] = {}
    for anomaly in anomalies:
        date_value = anomaly.get("date")
        median_value = anomaly.get("median")
        if isinstance(date_value, str) and isinstance(median_value, (int, float)):
            replacements[date_value] = median_value
    return [
        Observation(
            date=observation.date,
            views=replacements.get(observation.date.isoformat(), observation.views),
            series_id=observation.series_id,
            project=observation.project,
            article=observation.article,
        )
        for observation in observations
    ]


def _window_values(
    observations: Sequence[Observation], start: date, end: date
) -> list[int | float]:
    return [observation.views for observation in observations if start <= observation.date <= end]


def _growth_result(
    observations: Sequence[Observation], start: date, end: date, floor: int
) -> dict[str, object]:
    current_values = _window_values(observations, start, end)
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - (end - start)
    previous_values = _window_values(observations, previous_start, previous_end)
    if len(current_values) < floor or len(previous_values) < floor:
        return {
            "pct": None,
            "abs": None,
            "reason": INSUFFICIENT_OBSERVATIONS_REASON,
            "clean": {
                "pct": None,
                "abs": None,
                "reason": INSUFFICIENT_OBSERVATIONS_REASON,
            },
        }
    previous_mean = sum(
        (
            Fraction(value)
            if isinstance(value, int)
            else Fraction.from_float(value)
            for value in previous_values
        ),
        Fraction(0),
    ) / len(previous_values)
    if previous_mean == 0:
        return {
            "pct": None,
            "abs": None,
            "reason": ZERO_PREVIOUS_MEAN_REASON,
            "clean": {
                "pct": None,
                "abs": None,
                "reason": ZERO_PREVIOUS_MEAN_REASON,
            },
        }
    current_mean = sum(
        (
            Fraction(value)
            if isinstance(value, int)
            else Fraction.from_float(value)
            for value in current_values
        ),
        Fraction(0),
    ) / len(current_values)
    exact_pct = (current_mean / previous_mean - 1) * 100
    exact_abs = current_mean - previous_mean
    return {
        "pct": float(round(exact_pct, 1)),
        "abs": int(round(exact_abs)),
        "clean": {
            "pct": float(round(exact_pct, 1)),
            "abs": int(round(exact_abs)),
        },
    }


def compute_growth_for_days(
    observations: Sequence[Observation], end_date: date, days: int
) -> dict[str, object]:
    """Compute raw and median-replaced growth for one inclusive equal window."""
    if days <= 0:
        raise ValueError("days must be positive")
    start = end_date - timedelta(days=days - 1)
    floor = math.ceil(COVERAGE_RATIO * days)
    result = _growth_result(observations, start, end_date, floor)
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=days - 1)
    result.update(
        {
            "start": start.isoformat(),
            "end": end_date.isoformat(),
            "previous_start": previous_start.isoformat(),
            "previous_end": previous_end.isoformat(),
        }
    )
    return result


def _aligned_year_windows(
    observations: Sequence[Observation], end_date: date
) -> tuple[tuple[date, date], tuple[date, date]]:
    current_start = end_date - timedelta(days=365 - 1)
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=365 - 1)
    return (previous_start, previous_end), (current_start, end_date)


def _comparison_is_available(
    observations: Sequence[Observation], end_date: date, days: int
) -> bool:
    start = end_date - timedelta(days=days - 1)
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=days - 1)
    floor = math.ceil(COVERAGE_RATIO * days)
    current = _window_values(observations, start, end_date)
    previous = _window_values(observations, previous_start, previous_end)
    return (
        len(current) >= floor
        and len(previous) >= floor
        and sum(previous) > 0
    )


def comparison_crosses_methodology_break(
    observations: Sequence[Observation], end_date: date
) -> bool:
    """Return whether the computed y1 comparison spans both sides of the break."""
    if not _comparison_is_available(observations, end_date, 365):
        return False
    start = end_date - timedelta(days=365 - 1)
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=365 - 1)
    return previous_start < METHODOLOGY_BREAK < end_date


def _exact_value(value: int | float) -> Fraction:
    """Convert an accepted observation value to an exact rational, never a float."""
    if isinstance(value, int):
        return Fraction(value)
    return Fraction.from_float(value)


def _monthly_means(
    observations: Sequence[Observation], start: date, end: date
) -> dict[int, dict[str, Fraction]] | None:
    """Accumulate exact per-month sufficient statistics; None when a month is absent.

    The returned statistics gate the joint trend/calendar fit: a required calendar
    month with no observation in this half makes the model unidentifiable, so the
    caller must return the unavailable result instead of a partially identified fit.
    """
    statistics: dict[int, dict[str, Fraction]] = {
        month: {key: Fraction(0) for key in MONTH_STAT_KEYS} for month in range(1, 13)
    }
    for observation in observations:
        if not start <= observation.date <= end:
            continue
        offset = (observation.date - start).days
        views = _exact_value(observation.views)
        entry = statistics[observation.date.month]
        entry["rows"] += 1
        entry["x"] += offset
        entry["x2"] += offset * offset
        entry["x3"] += offset**3
        entry["x4"] += offset**4
        entry["y"] += views
        entry["xy"] += offset * views
        entry["x2y"] += offset * offset * views
    if any(entry["rows"] == 0 for entry in statistics.values()):
        return None
    return statistics


def _build_normal_equations(
    halves: Sequence[Mapping[int, Mapping[str, Fraction]]],
) -> tuple[list[list[Fraction]], list[Fraction]]:
    """Build the exact 17-parameter normal equations of the joint seasonal model.

    The design columns are the per-half ``1``, ``x``, and ``x*x`` trends plus, for
    each free month 1-11, the contrast ``1[month == m] - 1[month == 12]`` that
    encodes the zero-sum constraint. Every entry is a closed-form sum of the
    per-half/per-month sufficient statistics, so no per-observation matrix is built.
    """
    size = len(halves) * TREND_TERMS + FREE_MONTH_EFFECTS
    matrix = [[Fraction(0) for _ in range(size)] for _ in range(size)]
    right_hand_side = [Fraction(0) for _ in range(size)]
    month_totals = [
        sum((half[month]["rows"] for half in halves), Fraction(0))
        for month in range(1, 13)
    ]
    december_total = month_totals[11]
    for half_index, half in enumerate(halves):
        base = half_index * TREND_TERMS
        totals = [
            sum((half[month][key] for month in range(1, 13)), Fraction(0))
            for key in MONTH_STAT_KEYS
        ]
        rows, x1, x2, x3, x4, y0, y1, y2 = totals
        block = ((rows, x1, x2), (x1, x2, x3), (x2, x3, x4))
        for row_index in range(TREND_TERMS):
            matrix[base + row_index][base + row_index] = block[row_index][row_index]
        for row_index, column_index in ((0, 1), (0, 2), (1, 2)):
            matrix[base + row_index][base + column_index] = block[row_index][column_index]
            matrix[base + column_index][base + row_index] = block[row_index][column_index]
        right_hand_side[base] = y0
        right_hand_side[base + 1] = y1
        right_hand_side[base + 2] = y2
        december = half[12]
        for month in range(1, FREE_MONTH_EFFECTS + 1):
            column = MONTH_COLUMN_0 + month - 1
            entry = half[month]
            against_december = (
                entry["rows"] - december["rows"],
                entry["x"] - december["x"],
                entry["x2"] - december["x2"],
            )
            for row_index, value in enumerate(against_december):
                matrix[base + row_index][column] = value
                matrix[column][base + row_index] = value
            right_hand_side[column] += entry["y"] - december["y"]
    for month in range(1, FREE_MONTH_EFFECTS + 1):
        column = MONTH_COLUMN_0 + month - 1
        matrix[column][column] = month_totals[month - 1] + december_total
        for other in range(1, FREE_MONTH_EFFECTS + 1):
            if other == month:
                continue
            other_column = MONTH_COLUMN_0 + other - 1
            matrix[column][other_column] = december_total
            matrix[other_column][column] = december_total
    return matrix, right_hand_side


def _solve_exact_normal_equations(
    matrix: Sequence[Sequence[Fraction]], rhs: Sequence[Fraction]
) -> list[Fraction]:
    """Solve a dense square system exactly by pivoted Gaussian elimination.

    The fit never leaves exact rational arithmetic. A zero pivot means the
    normal-equation system is singular or non-identifiable, so the solve raises
    ``AnalysisError`` before any shared effect, pre-season residual, or selected
    month can be read; no partial fit is ever returned.
    """
    size = len(matrix)
    if size == 0:
        raise AnalysisError(
            "singular non-identifiable normal-equation system: no parameters to solve"
        )
    if any(len(row) != size for row in matrix) or len(rhs) != size:
        raise AnalysisError(
            "normal-equation matrix must be square and match its right-hand side"
        )
    rows = [[Fraction(value) for value in row] for row in matrix]
    values = [Fraction(value) for value in rhs]
    for column in range(size):
        pivot_row = max(
            range(column, size), key=lambda index: abs(rows[index][column])
        )
        if rows[pivot_row][column] == 0:
            raise AnalysisError(
                "singular non-identifiable normal-equation system: "
                f"zero pivot at column {column}"
            )
        rows[column], rows[pivot_row] = rows[pivot_row], rows[column]
        values[column], values[pivot_row] = values[pivot_row], values[column]
        pivot = rows[column][column]
        for index in range(column + 1, size):
            factor = rows[index][column] / pivot
            if factor == 0:
                continue
            rows[index][column] = Fraction(0)
            for position in range(column + 1, size):
                rows[index][position] -= factor * rows[column][position]
            values[index] -= factor * values[column]
    solution = [Fraction(0) for _ in range(size)]
    for index in range(size - 1, -1, -1):
        total = values[index]
        for position in range(index + 1, size):
            total -= rows[index][position] * solution[position]
        solution[index] = total / rows[index][index]
    return solution


@dataclass(frozen=True, slots=True)
class SharedMonthEffects:
    """Exact joint fit of per-half quadratic trends and shared calendar effects.

    ``shared_effects`` is ``s_m`` for every calendar month 1-12, including the
    derived zero-sum month 12. ``pre_effect_residual[month][half]`` is the mean of
    ``y - t_half(x)`` over that half's rows for the month, measured *before* the
    fitted shared effect is subtracted, so an exact repeated effect is never
    cancelled out of its own repeatability evidence.
    """

    shared_effects: dict[int, Fraction]
    pre_effect_residual: dict[int, dict[int, Fraction]]
    trend_coefficients: dict[int, tuple[Fraction, Fraction, Fraction]]


def _shared_month_effects(
    observations: Sequence[Observation], end_date: date
) -> SharedMonthEffects | None:
    """Fit per-half quadratic trends jointly with shared zero-sum month effects.

    Returns ``None`` when the complete-month gate rejects either aligned half, so a
    partially identified model is never attempted. January and December are ordinary
    month columns: no month is used as a trend anchor, so boundary effects stay
    estimable. This is an identifiable conservative separation for the documented
    two-cycle contract, not a universal decomposition theorem - a monotonic trend
    carrying month-scale structure outside the degree-2 daily trend family can
    still alias into calendar effects.
    """
    previous, current = _aligned_year_windows(observations, end_date)
    windows = (previous, current)
    halves = [_monthly_means(observations, *window) for window in windows]
    if any(half is None for half in halves):
        return None
    complete = cast("list[dict[int, dict[str, Fraction]]]", halves)
    matrix, rhs = _build_normal_equations(complete)
    solution = _solve_exact_normal_equations(matrix, rhs)
    shared_effects: dict[int, Fraction] = {
        month: solution[MONTH_COLUMN_0 + month - 1]
        for month in range(1, FREE_MONTH_EFFECTS + 1)
    }
    shared_effects[12] = -sum(shared_effects.values(), Fraction(0))
    trend_coefficients = {
        half_index: (
            solution[half_index * TREND_TERMS],
            solution[half_index * TREND_TERMS + 1],
            solution[half_index * TREND_TERMS + 2],
        )
        for half_index in range(SEASONALITY_HALVES)
    }
    residual_sums: dict[int, dict[int, Fraction]] = {
        month: {half_index: Fraction(0) for half_index in range(SEASONALITY_HALVES)}
        for month in range(1, 13)
    }
    residual_rows: dict[int, dict[int, int]] = {
        month: {half_index: 0 for half_index in range(SEASONALITY_HALVES)}
        for month in range(1, 13)
    }
    for half_index, (start, end) in enumerate(windows):
        constant, linear, quadratic = trend_coefficients[half_index]
        for observation in observations:
            if not start <= observation.date <= end:
                continue
            offset = (observation.date - start).days
            fitted = constant + linear * offset + quadratic * offset * offset
            entry = residual_sums[observation.date.month]
            entry[half_index] += _exact_value(observation.views) - fitted
            residual_rows[observation.date.month][half_index] += 1
    pre_effect_residual = {
        month: {
            half_index: residual_sums[month][half_index]
            / residual_rows[month][half_index]
            for half_index in range(SEASONALITY_HALVES)
        }
        for month in range(1, 13)
    }
    return SharedMonthEffects(
        shared_effects=shared_effects,
        pre_effect_residual=pre_effect_residual,
        trend_coefficients=trend_coefficients,
    )


def compute_seasonality(
    observations: Sequence[Observation], end_date: date
) -> dict[str, object]:
    """Find recurring peaks across two complete aligned 365-day halves.

    A month is reported only when its shared effect is strictly positive *and* the
    mean pre-season residual ``y - t_half(x)`` is strictly positive separately in
    both aligned halves. Selecting on ``y - t_half(x) - s_m`` would instead force an
    exact repeated effect to zero and wrongly reject it.
    """
    if not _comparison_is_available(observations, end_date, 365):
        return {"months": [], "note": SEASONALITY_UNAVAILABLE_NOTE}
    effects = _shared_month_effects(observations, end_date)
    if effects is None:
        return {"months": [], "note": SEASONALITY_UNAVAILABLE_NOTE}
    months = [
        month
        for month in range(1, 13)
        if effects.shared_effects[month] > 0
        and all(
            effects.pre_effect_residual[month][half_index] > 0
            for half_index in range(SEASONALITY_HALVES)
        )
    ]
    note = SEASONALITY_AVAILABLE_NOTE if months else SEASONALITY_NO_PEAKS_NOTE
    return {"months": months, "note": note}


def score_confidence(
    period_days: int,
    monthly_30d: int | float | Fraction,
    anomaly_share: float,
    clean_y1_available: bool,
    comparison_span: tuple[date, date] | None,
) -> tuple[str, list[str]]:
    """Score confidence with the frozen integer rubric and stable English reasons."""
    score = 0
    reasons: list[str] = []
    if period_days >= LONG_PERIOD_DAYS:
        score += 2
        reasons.append(PERIOD_LONG_REASON)
    elif period_days >= MIN_PERIOD_DAYS:
        score += 1
        reasons.append(PERIOD_MINIMUM_REASON)
    else:
        reasons.append(PERIOD_BELOW_MINIMUM_REASON)

    if monthly_30d >= HIGH_MONTHLY_VIEWS:
        score += 2
        reasons.append(VOLUME_HIGH_REASON)
    elif monthly_30d >= MIN_MONTHLY_VIEWS:
        score += 1
        reasons.append(VOLUME_MINIMUM_REASON)
    else:
        reasons.append(VOLUME_BELOW_MINIMUM_REASON)

    if anomaly_share == 0:
        score += 1
        reasons.append(NO_ANOMALIES_REASON)
    elif anomaly_share <= ANOMALY_SHARE_BOUNDARY:
        reasons.append(LIMITED_ANOMALIES_REASON)
    else:
        reasons.append(HIGH_ANOMALIES_REASON)

    if not clean_y1_available:
        reasons.append(MISSING_CLEAN_Y1_REASON)
    if comparison_span is not None:
        span_start, span_end = comparison_span
        if span_start < METHODOLOGY_BREAK <= span_end:
            score -= 2
            reasons.append(METHODOLOGY_CROSSING_REASON)

    level = "high" if score >= 4 else "medium" if score >= 2 else "low"
    if level == "low":
        reasons.append(LOW_CONFIDENCE_HYPOTHESIS_REASON)
    return level, reasons


def _direction_band(pct: float) -> str:
    if pct > DIRECTION_BAND_PCT:
        return "up"
    if pct < -DIRECTION_BAND_PCT:
        return "down"
    return "flat"


def safe_direction(
    period_days: int,
    raw_y1_pct: float | None,
    clean_y1_pct: float | None,
    confidence: str,
    monthly_30d: int | float | Fraction,
) -> str:
    """Apply period, confidence, volume, and raw/clean agreement safety gates."""
    if period_days < MIN_PERIOD_DAYS or clean_y1_pct is None:
        return "inconclusive"
    if confidence == "low" or monthly_30d < MIN_MONTHLY_VIEWS:
        return "noise"
    if raw_y1_pct is None:
        return "inconclusive"
    raw_band = _direction_band(raw_y1_pct)
    clean_band = _direction_band(clean_y1_pct)
    return clean_band if raw_band == clean_band else "noise"


def validate_finite_numbers(document: object) -> None:
    """Reject non-finite numbers recursively before JSON serialization."""
    if isinstance(document, float) and not math.isfinite(document):
        raise AnalysisError("analysis produced a non-finite number")
    if isinstance(document, dict):
        for value in document.values():
            validate_finite_numbers(value)
    elif isinstance(document, list):
        for value in document:
            validate_finite_numbers(value)


def _growth_for_output(
    observations: Sequence[Observation],
    clean_observations: Sequence[Observation],
    end_date: date,
    days: int,
) -> dict[str, object]:
    raw = _growth_result(
        observations,
        end_date - timedelta(days=days - 1),
        end_date,
        math.ceil(COVERAGE_RATIO * days),
    )
    clean = _growth_result(
        clean_observations,
        end_date - timedelta(days=days - 1),
        end_date,
        math.ceil(COVERAGE_RATIO * days),
    )
    result: dict[str, object] = {
        "pct": raw["pct"],
        "abs": raw["abs"],
        "start": (end_date - timedelta(days=days - 1)).isoformat(),
        "end": end_date.isoformat(),
        "clean": {
            "pct": clean["pct"],
            "abs": clean["abs"],
        },
    }
    if "reason" in raw:
        result["reason"] = raw["reason"]
        clean_result = result["clean"]
        if isinstance(clean_result, dict):
            clean_result["reason"] = clean["reason"]
    return result


def build_metrics(
    spec: Mapping[str, Any],
    spec_path: str | Path,
    grouped_observations: Mapping[str, Sequence[Observation]],
) -> dict[str, object]:
    """Build the exact frozen metrics document in spec series order."""
    spec_series = _as_spec_series(spec)
    all_dates: list[date] = []
    output_series: list[dict[str, object]] = []
    for series_id, expected in spec_series.items():
        observations = list(grouped_observations.get(series_id, []))
        if not observations:
            raise AnalysisError(f"no observations for series: {series_id}")
        start = observations[0].date
        end = observations[-1].date
        period_days = (end - start).days + 1
        total_views = sum(int(observation.views) for observation in observations)
        average_daily_exact = Fraction(total_views, period_days)
        monthly_30d_exact = average_daily_exact * 30
        avg_daily_views = round(float(average_daily_exact), 1)
        anomalies = detect_anomalies(observations)
        anomaly_share = len(anomalies) / len(observations)
        clean_observations = replace_anomalies(observations, anomalies)
        growth = {
            name: _growth_for_output(observations, clean_observations, end, days)
            for name, days in GROWTH_WINDOWS.items()
        }
        clean_y1 = growth["y1"]["clean"]
        clean_y1_available = isinstance(clean_y1, dict) and clean_y1.get("pct") is not None
        comparison_span = None
        if comparison_crosses_methodology_break(observations, end):
            y1_current_start = end - timedelta(days=365 - 1)
            y1_previous_start = y1_current_start - timedelta(days=365)
            comparison_span = (y1_previous_start, end)
        confidence, confidence_reasons = score_confidence(
            period_days,
            monthly_30d_exact,
            anomaly_share,
            clean_y1_available,
            comparison_span,
        )
        raw_y1 = growth["y1"].get("pct")
        clean_y1_pct = clean_y1.get("pct") if isinstance(clean_y1, dict) else None
        direction = safe_direction(
            period_days,
            raw_y1 if isinstance(raw_y1, (int, float)) else None,
            clean_y1_pct if isinstance(clean_y1_pct, (int, float)) else None,
            confidence,
            monthly_30d_exact,
        )
        output_series.append(
            {
                "series_id": series_id,
                "project": expected["project"],
                "article": expected["article"],
                "label": expected["label"],
                "language": expected["language"],
                "total_views": total_views,
                "avg_daily_views": avg_daily_views,
                "period": {"start": start.isoformat(), "end": end.isoformat(), "days": period_days},
                "growth": growth,
                "anomaly_share": anomaly_share,
                "trend_direction": direction,
                "confidence": confidence,
                "confidence_reasons": confidence_reasons,
                "seasonality": compute_seasonality(observations, end),
                "anomalies": anomalies,
            }
        )
        all_dates.extend(observation.date for observation in observations)
    document: dict[str, object] = {
        "spec_name": spec["name"],
        "as_of": max(all_dates).isoformat(),
        "generated_from": str(spec_path),
        "series": output_series,
    }
    validate_finite_numbers(document)
    return document


def main(argv: Sequence[str] | None = None) -> int:
    """Run the local-only analysis CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, help="path to spec.json")
    parser.add_argument("--out", default="out", help="output directory (default: out)")
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    spec = load_and_validate_spec(args.spec)
    out_dir = Path(args.out)
    try:
        grouped = load_series_csv(out_dir / "series.csv", spec)
        document = build_metrics(spec, args.spec, grouped)
        validate_finite_numbers(document)
        try:
            json.dumps(document, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise AnalysisError("metrics document is not JSON serializable") from error
        metrics_path = out_dir / "metrics.json"
        try:
            dump_json(document, metrics_path)
        except OSError as error:
            raise AnalysisError(f"could not write metrics output: {metrics_path}") from error
    except AnalysisError as error:
        print(f"analysis failed: {error}", file=sys.stderr)
        return 1
    series_count = len(cast(list[object], document["series"]))
    print(f"Analyzed {series_count} series; output: {metrics_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
