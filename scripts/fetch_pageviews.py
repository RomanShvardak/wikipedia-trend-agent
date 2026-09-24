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
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypedDict

import common
from common import (
    cache_key_for_url,
    cache_path_for_key,
    dump_json,
    load_and_validate_spec,
    setup_logging,
    user_agent,
)

log = common.log
CSV_HEADER = ("date", "views", "series_id", "project", "article")
Transport = Callable[[str, dict[str, str], float], "TransportResponse"]


class SeriesRow(TypedDict):
    date: str
    views: int
    series_id: str
    project: str
    article: str


class FetchError(RuntimeError):
    """A model-readable fetch or response-validation failure."""


class FetchTransportError(FetchError):
    """The transport could not complete an HTTP exchange."""


@dataclass
class TransportResponse:
    status: int
    headers: dict[str, str]
    body: bytes


def default_transport(
    url: str,
    headers: dict[str, str],
    timeout: float = 30.0,
) -> TransportResponse:
    """Perform one urllib GET and normalize HTTP errors into responses."""
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return TransportResponse(
                status=int(response.status),
                headers=dict(response.headers),
                body=response.read(),
            )
    except urllib.error.HTTPError as error:
        return TransportResponse(
            status=int(error.code),
            headers=dict(error.headers) if error.headers is not None else {},
            body=error.read(),
        )
    except OSError as error:
        raise FetchTransportError(str(error)) from error


def series_url(project: str, article: str, start_ymd: str, end_ymd: str) -> str:
    """Build the AQS per-article URL with required 10-digit timestamps."""
    return (
        "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
        f"{project}/all-access/user/{article}/daily/{start_ymd}00/{end_ymd}00"
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
    series_id: str,
) -> dict[str, Any] | None:
    cache_path = cache_path_for_key(cache_key_for_url(url))
    if not cache_path.exists():
        return None

    if cache_fresh(cache_path, ttl_hours):
        try:
            envelope = json.loads(cache_path.read_text(encoding="utf-8"))
            return envelope["response"]
        except (OSError, TypeError, KeyError, json.JSONDecodeError):
            pass

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


def _parse_response(body: bytes, series: Mapping[str, Any]) -> list[SeriesRow]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FetchError("response body is not valid UTF-8 JSON") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise FetchError("response body must be an AQS object with an items list")

    rows: list[SeriesRow] = []
    for index, item in enumerate(payload["items"]):
        if not isinstance(item, dict):
            raise FetchError(f"item {index} must be an object")
        timestamp = item.get("timestamp")
        if (
            not isinstance(timestamp, str)
            or len(timestamp) != 10
            or not timestamp.isdigit()
        ):
            raise FetchError(f"item {index} has an invalid timestamp")
        raw_views = item.get("views")
        if isinstance(raw_views, bool) or not isinstance(raw_views, (int, str)):
            raise FetchError(f"item {index} has an invalid views value")
        try:
            date = datetime.strptime(timestamp[:8], "%Y%m%d").date().isoformat()
            views = int(raw_views)
        except (TypeError, ValueError) as error:
            raise FetchError(f"item {index} has an invalid date or views value") from error
        rows.append(
            {
                "date": date,
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
) -> tuple[list[SeriesRow], bool]:
    url = series_url(series["project"], series["article"], start_ymd, end_ymd)
    cached = _read_cached_response(url, ttl_hours, series["id"])
    if cached is not None:
        cache_hit = True
        payload_bytes = json.dumps(cached, ensure_ascii=False).encode("utf-8")
    else:
        common.throttle(1.0, pace)
        response = transport(url, headers, 30.0)
        cache_hit = False
        if response.status != 200:
            raise FetchError(f"unexpected HTTP {response.status}")
        payload_bytes = response.body

    rows = _parse_response(payload_bytes, series)
    if not cache_hit:
        parsed_payload = json.loads(payload_bytes.decode("utf-8"))
        cache_path = cache_path_for_key(cache_key_for_url(url))
        dump_json(
            {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "status": 200,
                "response": parsed_payload,
            },
            cache_path,
        )
    return rows, cache_hit


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

    try:
        for series in spec["series"]:
            series_rows, cache_hit = _fetch_series(
                series=series,
                start_ymd=window["start"],
                end_ymd=window["end"],
                ttl_hours=args.ttl_hours,
                headers=headers,
                transport=request_transport,
                pace=fetch_pace,
            )
            rows.extend(series_rows)
            cache_hits += int(cache_hit)
            log.debug("%s: fetched %d item(s)", series["id"], len(series_rows))
    except FetchError as error:
        print(f"fetch_pageviews: {error}", file=sys.stderr)
        return 1

    output = _write_series_csv(args.out, rows)
    date_values = [row["date"] for row in rows]
    date_range = f"{min(date_values)}..{max(date_values)}" if date_values else "empty"
    print(
        f"Fetched {len(rows)} row(s) for {len(spec['series'])} series "
        f"({date_range}; cache hits: {cache_hits}/{len(spec['series'])}; output: {output})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
