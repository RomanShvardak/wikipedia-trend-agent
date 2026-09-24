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
    "recurring peaks are above the monthly median in both aligned years"
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
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise AnalysisError(f"row {row_number}: date must be YYYY-MM-DD") from error


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

    try:
        text = csv_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise AnalysisError("series CSV must be valid UTF-8") from error
    except OSError as error:
        raise AnalysisError(f"could not read series CSV: {csv_path}") from error

    reader = csv.reader(text.splitlines())
    try:
        header = next(reader)
    except StopIteration as error:
        raise AnalysisError("series CSV is empty") from error
    if tuple(header) != CSV_HEADER:
        raise AnalysisError(f"series CSV header must be exactly {','.join(CSV_HEADER)}")

    spec_series = _as_spec_series(spec)
    grouped: dict[str, list[Observation]] = {series_id: [] for series_id in spec_series}
    seen: set[tuple[str, date]] = set()
    row_count = 0
    for row_number, row in enumerate(reader, start=2):
        row_count += 1
        if row_count > MAX_OBSERVATIONS:
            raise AnalysisError(f"series CSV exceeds {MAX_OBSERVATIONS} observations")
        if len(row) != len(CSV_HEADER):
            raise AnalysisError(f"row {row_number}: expected {len(CSV_HEADER)} CSV fields")
        date_text, views_text, series_id, project, article = row
        if series_id not in spec_series:
            raise AnalysisError(f"row {row_number}: series_id is not present in spec: {series_id}")
        expected = spec_series[series_id]
        if project != expected.get("project") or article != expected.get("article"):
            raise AnalysisError(f"row {row_number}: project/article do not match spec for {series_id}")
        if not views_text.isdigit():
            raise AnalysisError(f"row {row_number}: views must be a non-negative integer")
        views = int(views_text)
        observed_date = _parse_iso_date(date_text, row_number)
        identity = (series_id, observed_date)
        if identity in seen:
            raise AnalysisError(f"row {row_number}: duplicate date for series {series_id}")
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
    previous_mean = sum(previous_values) / len(previous_values)
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
    current_mean = sum(current_values) / len(current_values)
    return {
        "pct": round((current_mean / previous_mean - 1) * 100, 1),
        "abs": int(round(current_mean - previous_mean)),
        "clean": {
            "pct": round((current_mean / previous_mean - 1) * 100, 1),
            "abs": int(round(current_mean - previous_mean)),
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


def _monthly_means(
    observations: Sequence[Observation], start: date, end: date
) -> list[float]:
    values: dict[int, list[int | float]] = {month: [] for month in range(1, 13)}
    for observation in observations:
        if start <= observation.date <= end:
            values[observation.date.month].append(observation.views)
    return [
        sum(month_values) / len(month_values) if month_values else 0.0
        for month_values in values.values()
    ]


def compute_seasonality(
    observations: Sequence[Observation], end_date: date
) -> dict[str, object]:
    """Find recurring peaks across two complete aligned 365-day halves."""
    previous, current = _aligned_year_windows(observations, end_date)
    if not _comparison_is_available(observations, end_date, 365):
        return {"months": [], "note": SEASONALITY_UNAVAILABLE_NOTE}
    previous_means = _monthly_means(observations, *previous)
    current_means = _monthly_means(observations, *current)
    previous_median = median(previous_means)
    current_median = median(current_means)
    months = [
        month
        for month, (previous_mean, current_mean) in enumerate(
            zip(previous_means, current_means), start=1
        )
        if previous_mean > previous_median and current_mean > current_median
    ]
    note = SEASONALITY_AVAILABLE_NOTE if months else SEASONALITY_NO_PEAKS_NOTE
    return {"months": months, "note": note}


def score_confidence(
    period_days: int,
    monthly_30d: float,
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
    monthly_30d: float,
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
        total_views = sum(observation.views for observation in observations)
        avg_daily_views = round(total_views / period_days, 1)
        monthly_30d = avg_daily_views * 30
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
            monthly_30d,
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
            monthly_30d,
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
        json.dumps(document, ensure_ascii=False, allow_nan=False)
        metrics_path = out_dir / "metrics.json"
        dump_json(document, metrics_path)
    except AnalysisError as error:
        print(f"analysis failed: {error}", file=sys.stderr)
        return 1
    series_count = len(cast(list[object], document["series"]))
    print(f"Analyzed {series_count} series; output: {metrics_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
