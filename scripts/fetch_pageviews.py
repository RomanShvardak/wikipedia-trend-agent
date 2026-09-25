"""Fetch Wikipedia pageviews for one global spec window and write series.csv.

Uses the Phase 1 validation, User-Agent, cache-key, JSON, and throttle helpers
through an injectable urllib transport. The tracer writes flat daily rows with
the exact D-13/D-14 CSV contract (D-19 CLI flags).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypedDict

import common
from common import (
    Transport,
    TransportError,
    TransportResponse,
    cache_key_for_url,
    cache_path_for_key,
    default_transport,
    dump_json,
    header_value,
    is_retryable_status,
    load_and_validate_spec,
    retry_after_seconds,
    setup_logging,
    user_agent,
)

log = common.log
CSV_HEADER = ("date", "views", "series_id", "project", "article")
NOT_LOADED_GAP_DAYS = 2
MAX_FETCH_ATTEMPTS = 3
RETRY_FALLBACK_SECONDS = 5.0
sleep = time.sleep


class SeriesRow(TypedDict):
    date: str
    views: int
    series_id: str
    project: str
    article: str


class FetchError(RuntimeError):
    """A model-readable fetch or response-validation failure."""


FetchTransportError = TransportError


class SeriesFetchError(FetchError):
    """A fatal response tied to a specific series."""

    def __init__(self, series_id: str, message: str) -> None:
        super().__init__(message)
        self.series_id = series_id


def series_url(project: str, article: str, start_ymd: str, end_ymd: str) -> str:
    """Build the AQS per-article URL with required 10-digit timestamps."""
    return (
        "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
        f"{project}/all-access/user/{article}/daily/{start_ymd}00/{end_ymd}00"
    )


def _parse_ymd(value: str) -> date:
    """Parse the spec's YYYYMMDD or an ISO date used by direct helper tests."""
    if len(value) == 8:
        return datetime.strptime(value, "%Y%m%d").date()
    return date.fromisoformat(value)


def chunk_ranges(start_ymd: str, end_ymd: str) -> list[tuple[str, str]]:
    """Split an inclusive date window into contiguous <=365-day chunks.

    The returned list is chronological: each chunk is at most 365 inclusive
    days, the final chunk may be shorter, and adjacent chunks share no day and
    leave no gap.
    """
    start = _parse_ymd(start_ymd)
    end = _parse_ymd(end_ymd)
    if start > end:
        raise ValueError("window start after end")

    ranges: list[tuple[str, str]] = []
    chunk_start = start
    while chunk_start <= end:
        day_count = min(365, (end - chunk_start).days + 1)
        chunk_end = chunk_start + timedelta(days=day_count - 1)
        ranges.append((chunk_start.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d")))
        chunk_start = chunk_end + timedelta(days=1)
    return ranges


def effective_end(spec_end_ymd: str, yesterday_utc: date) -> str:
    """Clamp a spec end to the last complete UTC day and return YYYYMMDD."""
    return min(_parse_ymd(spec_end_ymd), yesterday_utc).strftime("%Y%m%d")


def classify_404(window_end_ymd: str, yesterday_utc: date) -> str:
    """Classify a 404 using only its distance from the last complete UTC day."""
    gap = (yesterday_utc - _parse_ymd(window_end_ymd)).days
    return "not_loaded" if gap <= NOT_LOADED_GAP_DAYS else "no_views"


def _zero_rows(
    series: Mapping[str, Any],
    start_ymd: str,
    end_ymd: str,
) -> list[SeriesRow]:
    """Create one truthful zero row for every day in the requested window."""
    start = _parse_ymd(start_ymd)
    end = _parse_ymd(end_ymd)
    return [
        {
            "date": current.isoformat(),
            "views": 0,
            "series_id": series["id"],
            "project": series["project"],
            "article": series["article"],
        }
        for current in _date_range(start, end)
    ]


def _date_range(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _diagnostic(series_id: str, message: str) -> None:
    """Write one grep-friendly per-series diagnostic to stderr."""
    print(f"{series_id}: {message}", file=sys.stderr)


def _print_summary(
    series_count: int,
    failures: int,
    cache_hits: int,
    rows: list[SeriesRow],
    output: Path | None,
    request_count: int | None = None,
) -> None:
    date_values = [row["date"] for row in rows]
    date_range = f"{min(date_values)}..{max(date_values)}" if date_values else "empty"
    output_text = f"; output: {output}" if output is not None else ""
    cache_denominator = series_count if request_count is None else request_count
    print(
        f"Fetched {len(rows)} row(s) for {series_count} series "
        f"({date_range}; failures: {failures}; cache hits: {cache_hits}/{cache_denominator}{output_text})"
    )


def cache_fresh(path: Path, ttl_hours: int) -> bool:
    """Return whether a cache envelope is fresh and structurally usable."""
    if ttl_hours <= 0:
        return False
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(envelope, dict)
            or envelope.get("status") != 200
            or not isinstance(envelope.get("response"), dict)
            or not isinstance(envelope["response"].get("items"), list)
        ):
            return False
        fetched_at = datetime.fromisoformat(envelope["fetched_at"])
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - fetched_at <= timedelta(hours=ttl_hours)
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return False


def _read_cached_response(
    url: str,
    ttl_hours: int,
    series: Mapping[str, Any],
    start_ymd: str,
    end_ymd: str,
) -> list[SeriesRow] | None:
    series_id = series["id"]
    cache_path = cache_path_for_key(cache_key_for_url(url))
    if not cache_path.exists():
        return None

    if cache_fresh(cache_path, ttl_hours):
        try:
            envelope = json.loads(cache_path.read_text(encoding="utf-8"))
            payload_bytes = json.dumps(
                envelope["response"], ensure_ascii=False
            ).encode("utf-8")
            return _parse_response(payload_bytes, series, start_ymd, end_ymd)
        except (OSError, TypeError, KeyError, json.JSONDecodeError, FetchError):
            print(
                f"{series_id}: invalid cache entry; refetching from network",
                file=sys.stderr,
            )
            try:
                cache_path.unlink(missing_ok=True)
            except OSError as error:
                print(
                    f"{series_id}: could not remove invalid cache entry: {error}",
                    file=sys.stderr,
                )
            return None

    try:
        envelope = json.loads(cache_path.read_text(encoding="utf-8"))
        response = envelope.get("response") if isinstance(envelope, dict) else None
        is_corrupt = not (
            isinstance(envelope, dict)
            and envelope.get("status") == 200
            and isinstance(response, dict)
            and isinstance(response.get("items"), list)
        )
    except (OSError, TypeError, json.JSONDecodeError):
        is_corrupt = True

    if is_corrupt:
        print(
            f"{series_id}: corrupt cache entry; refetching from network",
            file=sys.stderr,
        )
        try:
            cache_path.unlink(missing_ok=True)
        except OSError as error:
            print(f"{series_id}: could not remove corrupt cache entry: {error}", file=sys.stderr)
    return None


def _parse_response(
    body: bytes,
    series: Mapping[str, Any],
    start_ymd: str,
    end_ymd: str,
) -> list[SeriesRow]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FetchError("response body is not valid UTF-8 JSON") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise FetchError("response body must be an AQS object with an items list")

    start_date = _parse_ymd(start_ymd)
    end_date = _parse_ymd(end_ymd)
    rows: list[SeriesRow] = []
    for index, item in enumerate(payload["items"]):
        if not isinstance(item, dict):
            raise FetchError(f"item {index} must be an object")
        timestamp = item.get("timestamp")
        if (
            not isinstance(timestamp, str)
            or len(timestamp) != 10
            or not timestamp.isdigit()
            or timestamp[-2:] != "00"
        ):
            raise FetchError(f"item {index} has an invalid timestamp")
        raw_views = item.get("views")
        if isinstance(raw_views, bool) or not isinstance(raw_views, (int, str)):
            raise FetchError(f"item {index} has an invalid views value")
        try:
            item_date = datetime.strptime(timestamp[:8], "%Y%m%d").date()
            views = int(raw_views)
        except (TypeError, ValueError) as error:
            raise FetchError(f"item {index} has an invalid date or views value") from error
        if not start_date <= item_date <= end_date:
            raise FetchError(f"item {index} is outside the requested chunk")
        if views < 0:
            raise FetchError(f"item {index} has a negative views value")
        rows.append(
            {
                "date": item_date.isoformat(),
                "views": views,
                "series_id": series["id"],
                "project": series["project"],
                "article": series["article"],
            }
        )
    return rows


def _fetch_series(
    series: Mapping[str, Any],
    start_ymd: str,
    end_ymd: str,
    ttl_hours: int,
    headers: dict[str, str],
    transport: Transport,
    pace: list[float],
    yesterday_utc: date,
) -> tuple[list[SeriesRow], bool, str]:
    url = series_url(series["project"], series["article"], start_ymd, end_ymd)
    cached_rows = _read_cached_response(
        url, ttl_hours, series, start_ymd, end_ymd
    )
    if cached_rows is not None:
        return cached_rows, True, "ok"

    payload_bytes = b""
    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
            common.throttle(1.0, pace)
            try:
                response = transport(url, headers, 30.0)
            except FetchTransportError:
                if attempt < MAX_FETCH_ATTEMPTS:
                    sleep(RETRY_FALLBACK_SECONDS)
                    continue
                message = f"request failed after {attempt} attempt(s) (transport error)"
                _diagnostic(series["id"], message)
                return [], False, "failed"
            if response.status == 403:
                message = "HTTP 403 — set a real WTI_USER_AGENT contact and retry"
                _diagnostic(series["id"], message)
                raise SeriesFetchError(series["id"], message)
            if response.status == 404:
                classification = classify_404(end_ymd, yesterday_utc)
                if classification == "not_loaded":
                    _diagnostic(series["id"], "data not yet loaded — retry later")
                else:
                    _diagnostic(series["id"], "no views for the requested window")
                if classification == "not_loaded":
                    return [], False, "not_loaded"
                return _zero_rows(series, start_ymd, end_ymd), False, "no_views"
            if is_retryable_status(response.status) and attempt < MAX_FETCH_ATTEMPTS:
                sleep(retry_after_seconds(response.headers))
                continue
            if response.status != 200:
                message = f"request failed after {attempt} attempt(s) (HTTP {response.status})"
                _diagnostic(series["id"], message)
                return [], False, "failed"
            payload_bytes = response.body
            break

    rows = _parse_response(payload_bytes, series, start_ymd, end_ymd)
    parsed_payload = json.loads(payload_bytes.decode("utf-8"))
    common.write_json_cache(url, parsed_payload)
    return rows, False, "ok"


def _write_series_csv(out_dir: str | Path, rows: list[SeriesRow]) -> Path:
    output = Path(out_dir) / "series.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        writer.writerows(
            [row[column] for column in CSV_HEADER]
            for row in sorted(rows, key=lambda row: (row["series_id"], row["date"]))
        )
    return output


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: Transport | None = None,
    pace: list[float] | None = None,
    today_utc: date | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, help="path to spec.json")
    parser.add_argument("--out", default="out", help="output directory (default: out)")
    parser.add_argument(
        "--ttl-hours",
        type=int,
        default=int(os.environ.get("WTI_TTL_HOURS", 24)),
        help="cache TTL in hours (default: WTI_TTL_HOURS or 24)",
    )
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    spec = load_and_validate_spec(args.spec)
    ua = user_agent()
    headers = {"User-Agent": ua, "Accept": "application/json"}
    request_transport = transport or default_transport
    fetch_pace = pace if pace is not None else [0.0]
    window = spec["window"]
    rows: list[SeriesRow] = []
    cache_hits = 0
    failures = 0
    request_count = 0
    today = today_utc or datetime.now(timezone.utc).date()
    yesterday_utc = today - timedelta(days=1)
    fail_on_empty_series = bool(spec.get("quality", {}).get("fail_on_empty_series", False))
    clamped_end = effective_end(window["end"], yesterday_utc)

    for series in spec["series"]:
        try:
            if window["start"] > clamped_end:
                _diagnostic(series["id"], "no fetchable days after UTC clamp")
                _print_summary(len(spec["series"]), failures + 1, cache_hits, rows, None, request_count)
                return 1

            chunks = chunk_ranges(window["start"], clamped_end)
            series_rows: list[SeriesRow] = []
            series_failed = False
            successful_chunks = 0
            not_found_chunks = 0
            for chunk_start, chunk_end in chunks:
                request_count += 1
                chunk_rows, cache_hit, outcome = _fetch_series(
                    series=series,
                    start_ymd=chunk_start,
                    end_ymd=chunk_end,
                    ttl_hours=args.ttl_hours,
                    headers=headers,
                    transport=request_transport,
                    pace=fetch_pace,
                    yesterday_utc=yesterday_utc,
                )
                series_rows.extend(chunk_rows)
                cache_hits += int(cache_hit)
                if outcome == "ok":
                    successful_chunks += 1
                elif outcome in {"not_loaded", "no_views"}:
                    not_found_chunks += 1
                elif outcome == "failed":
                    series_failed = True

            if (
                fail_on_empty_series
                and successful_chunks == 0
                and not_found_chunks > 0
            ):
                _print_summary(len(spec["series"]), failures + 1, cache_hits, rows, None, request_count)
                return 1
        except SeriesFetchError:
            _print_summary(len(spec["series"]), failures + 1, cache_hits, rows, None, request_count)
            return 1
        except FetchError as error:
            _diagnostic(series["id"], str(error))
            _print_summary(len(spec["series"]), failures + 1, cache_hits, rows, None, request_count)
            return 1

        failures += int(series_failed)
        rows.extend(series_rows)

    if failures == len(spec["series"]):
        _print_summary(len(spec["series"]), failures, cache_hits, rows, None, request_count)
        return 1

    output = _write_series_csv(args.out, rows)
    _print_summary(len(spec["series"]), failures, cache_hits, rows, output, request_count)
    return 3 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
