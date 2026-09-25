"""Shared utilities for the wikipedia-trend-agent pipeline.

Logging, spec/JSON I/O, strict aggregated spec validation, .cache/ sha256
key design, User-Agent fail-fast, and a ~1 req/s throttle. Stdlib only —
the data layer stays pandas/requests/numpy-free by design (AGENTS.md).

CLI stages import from this module: fetch_pageviews, resolve_articles,
analyze_trends, make_charts, build_report, run_all.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

DEFAULT_UA = "wikipedia-trend-agent/0.1.0 (contact@example.org) python-urllib"
CACHE_DIR = Path(os.environ.get("WTI_CACHE", ".cache"))
RETRY_FALLBACK_SECONDS = 5.0
log = logging.getLogger("wta")

_DATE_RE = re.compile(r"^\d{8}$")
_ALLOWED_ROOT_KEYS = {"name", "request", "language", "window", "series", "assumptions", "quality"}
_ALLOWED_WINDOW_KEYS = {"start", "end", "granularity"}
_ALLOWED_SERIES_KEYS = {"id", "project", "article", "label", "language"}
_ALLOWED_QUALITY_KEYS = {"fail_on_empty_series"}
_VALID_GRANULARITIES = {"daily"}
_SERIES_FIELD_FIX = {
    "id": 'add "id" like "pl-post-przerywany"',
    "project": 'add "project" like "pl.wikipedia"',
    "article": 'add "article" like "Post_przerywany"',
    "label": 'add "label" like "Польська: інтервальне голодування"',
    "language": 'add "language" like "pl"',
}
# Shared default throttle state: consecutive bare throttle() calls are spaced.
# Callers may inject their own list for per-client pacing (Phase 2 fetch).
_THROTTLE_LAST: list[float] = [0.0]


@dataclass
class TransportResponse:
    """A normalized HTTP exchange consumed by endpoint-specific clients."""

    status: int
    headers: dict[str, str]
    body: bytes


Transport = Callable[[str, dict[str, str], float], "TransportResponse"]


class TransportError(RuntimeError):
    """The transport could not complete an HTTP exchange."""


class ResponseTooLarge(TransportError):
    """The response exceeds the caller's explicit byte bound."""


def _validated_max_bytes(max_bytes: int | None) -> int | None:
    if max_bytes is None:
        return None
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        raise TypeError("max_bytes must be a positive integer or None")
    if max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    return max_bytes


def _read_response_body(stream: Any, max_bytes: int | None) -> bytes:
    if max_bytes is None:
        return stream.read()

    declared = header_value(dict(getattr(stream, "headers", {}) or {}), "Content-Length")
    if declared is not None:
        try:
            declared_length = int(declared)
        except (TypeError, ValueError):
            declared_length = -1
        if declared_length > max_bytes:
            raise ResponseTooLarge(
                f"response Content-Length {declared_length} exceeds {max_bytes} bytes"
            )

    body = stream.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ResponseTooLarge(f"response body exceeds {max_bytes} bytes")
    return body


def default_transport(
    url: str,
    headers: dict[str, str],
    timeout: float = 30.0,
    *,
    max_bytes: int | None = None,
) -> TransportResponse:
    """Perform one urllib GET, optionally bounding response allocation."""
    byte_limit = _validated_max_bytes(max_bytes)
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return TransportResponse(
                status=int(response.status),
                headers=dict(response.headers),
                body=_read_response_body(response, byte_limit),
            )
    except urllib.error.HTTPError as error:
        return TransportResponse(
            status=int(error.code),
            headers=dict(error.headers) if error.headers is not None else {},
            body=_read_response_body(error, byte_limit),
        )
    except OSError as error:
        raise TransportError(str(error)) from error


def header_value(headers: Mapping[str, Any], name: str) -> str | None:
    """Read a response header case-insensitively without exposing its value."""
    wanted = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == wanted and isinstance(value, str):
            return value
    return None


def retry_after_seconds(
    headers: Mapping[str, Any],
    now: datetime | None = None,
) -> float:
    """Parse Retry-After seconds or an HTTP-date with a safe five-second fallback."""
    raw = header_value(headers, "Retry-After")
    if raw is None:
        return RETRY_FALLBACK_SECONDS

    value = raw.strip()
    if value.isdigit():
        seconds = int(value)
        return float(seconds) if seconds > 0 else RETRY_FALLBACK_SECONDS

    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return RETRY_FALLBACK_SECONDS
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return max(0.0, (retry_at - current).total_seconds())


def is_retryable_status(status: int) -> bool:
    """Return whether an endpoint should apply the bounded retry policy."""
    return status == 429 or 500 <= status <= 599


def read_json_cache(
    url: str,
    ttl_hours: float,
    validate: Callable[[dict[str, object]], object | None],
) -> object | None:
    """Return a fresh validator-approved HTTP 200 payload or a safe miss."""
    if ttl_hours <= 0:
        return None
    path = cache_path_for_key(cache_key_for_url(url))
    if not path.exists():
        return None

    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(envelope, dict)
            or envelope.get("status") != 200
            or not isinstance(envelope.get("response"), dict)
            or not isinstance(envelope.get("fetched_at"), str)
        ):
            raise ValueError("invalid cache envelope")
        fetched_at = datetime.fromisoformat(envelope["fetched_at"])
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        response = envelope["response"]
        if not isinstance(response, dict):
            raise ValueError("cache response must be an object")
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        _remove_cache_path(path)
        return None

    try:
        validated = validate(response)
    except (TypeError, ValueError, KeyError):
        _remove_cache_path(path)
        return None
    if validated is None:
        _remove_cache_path(path)
        return None
    if datetime.now(timezone.utc) - fetched_at > timedelta(hours=ttl_hours):
        return None
    return validated


def _remove_cache_path(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.warning("could not remove invalid cache entry %s", path.name)


def write_json_cache(
    url: str,
    payload: dict[str, object],
    *,
    fetched_at: datetime | None = None,
) -> Path:
    """Atomically publish a validated HTTP 200 JSON cache envelope."""
    path = cache_path_for_key(cache_key_for_url(url))
    timestamp = fetched_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    dump_json(
        {
            "fetched_at": timestamp.isoformat(),
            "status": 200,
            "response": payload,
        },
        path,
    )
    return path


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )


def user_agent() -> str:
    """Descriptive User-Agent or fail-fast (D-04).

    Wikimedia 403s clients without a descriptive UA. Raises SystemExit when
    the value is empty, whitespace-only, or still carries the placeholder
    ("contact" / "example.org") — so a non-descriptive identity never leaks.
    """
    ua = os.environ.get("WTI_USER_AGENT", DEFAULT_UA)
    if not ua or ua.strip() == "" or "contact" in ua or "example.org" in ua:
        raise SystemExit(
            "User-Agent is empty or uses placeholder; set WTI_USER_AGENT to a descriptive value "
            "(e.g. 'wikipedia-trend-agent/0.1.0 (you@example.com) python-urllib')"
        )
    return ua


def load_spec(path: str | Path) -> dict[str, Any]:
    """Read and parse a spec.json. Raises SystemExit on read/JSON errors."""
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise SystemExit(f"Failed to load spec {p}: {e}") from e
    if not isinstance(data, dict):
        raise SystemExit(f"Failed to load spec {p}: top-level value must be a JSON object, got {type(data).__name__}")
    return data


def dump_json(obj: Any, path: str | Path) -> None:
    """Atomically write inspectable UTF-8 JSON through a same-directory staging file."""
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
            handle.write(json.dumps(obj, ensure_ascii=False, indent=2))
            handle.flush()
        os.replace(temporary_path, p)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _is_valid_date(value: Any) -> bool:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        return False
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return False
    return True


def validate_spec(spec: dict[str, Any]) -> list[str]:
    """Strict AGGREGATED validation of the frozen spec.json contract (D-01/D-02/D-07).

    Collects ALL violations (never first-error fast-fail) as model-readable
    lines of the form ``{path}: {problem} (how to fix: {fix})``. Never raises;
    callers decide what to do with the list.
    """
    errors: list[str] = []
    if not isinstance(spec, dict):
        return [
            f"root: expected a JSON object, got {type(spec).__name__} "
            "(how to fix: write the spec as one top-level object)"
        ]

    # --- root required fields ---
    for field, example in (("name", "intermittent_fasting_pl_cs"), ("request", "a free-text request"), ("language", "uk")):
        if field not in spec:
            errors.append(f'root.{field}: missing required field (how to fix: add "{field}" like "{example}")')
        elif not isinstance(spec[field], str) or spec[field].strip() == "":
            errors.append(f"root.{field}: must be a non-empty string (how to fix: provide a non-empty {field})")

    # --- root unknown keys ---
    for key in spec:
        if key not in _ALLOWED_ROOT_KEYS:
            errors.append(f'unknown field "{key}" at root (how to fix: remove or move to allowed location)')

    # --- window ---
    window = spec.get("window")
    if isinstance(window, dict):
        for key in window:
            if key not in _ALLOWED_WINDOW_KEYS:
                errors.append(f'unknown field "{key}" in window (how to fix: remove or move to allowed location)')
        start = window.get("start")
        end = window.get("end")
        if start is None:
            errors.append('window.start: missing required field (how to fix: add "start" like "20240923")')
        elif not _is_valid_date(start):
            errors.append(f"window.start: invalid format {start!r} expected YYYYMMDD (how to fix: use \"YYYYMMDD\")")
        if end is None:
            errors.append('window.end: missing required field (how to fix: add "end" like "20260920")')
        elif not _is_valid_date(end):
            errors.append(f"window.end: invalid format {end!r} expected YYYYMMDD (how to fix: use \"YYYYMMDD\")")
        if isinstance(start, str) and isinstance(end, str) and _is_valid_date(start) and _is_valid_date(end):
            if start >= end:
                errors.append(
                    f"window: start ({start}) must be before end ({end}) (how to fix: swap so start < end)"
                )
        granularity = window.get("granularity", "daily")
        if not isinstance(granularity, str) or granularity not in _VALID_GRANULARITIES:
            errors.append(
                f'window.granularity: invalid value {granularity!r}, allowed: "daily" in v1 (how to fix: use "daily")'
            )
    elif window is not None:
        errors.append("window: expected an object with start/end (how to fix: use the window shape from the contract)")

    # --- series[] ---
    series = spec.get("series")
    if series is None:
        errors.append('root.series: missing required field (how to fix: add "series" as a non-empty list)')
    elif not isinstance(series, list):
        errors.append('root.series: expected a list (how to fix: make "series" a list of objects)')
    elif len(series) == 0:
        errors.append('root.series: must be a non-empty list (how to fix: add at least one series item)')
    else:
        for idx, item in enumerate(series):
            path = f"series[{idx}]"
            if not isinstance(item, dict):
                errors.append(
                    f"{path}: expected an object with id/project/article/label/language "
                    "(how to fix: use the series item shape)"
                )
                continue
            for key in item:
                if key not in _ALLOWED_SERIES_KEYS:
                    errors.append(f'unknown field "{key}" at {path} (how to fix: remove or move to allowed location)')
            for field in ("id", "project", "article", "label", "language"):
                if field not in item:
                    errors.append(f"{path}.{field}: missing required field (how to fix: {_SERIES_FIELD_FIX[field]})")
                elif not isinstance(item[field], str) or item[field].strip() == "":
                    errors.append(f"{path}.{field}: must be a non-empty string (how to fix: provide a non-empty {field})")

    # --- assumptions (optional list of str) ---
    assumptions = spec.get("assumptions")
    if assumptions is not None:
        if not isinstance(assumptions, list) or not all(isinstance(a, str) for a in assumptions):
            errors.append("root.assumptions: expected a list of strings (how to fix: make assumptions a list of strings)")

    # --- quality (optional dict) ---
    quality = spec.get("quality")
    if quality is not None:
        if not isinstance(quality, dict):
            errors.append(
                'root.quality: expected an object (how to fix: use `{"fail_on_empty_series": true}`)'
            )
        else:
            for key in quality:
                if key not in _ALLOWED_QUALITY_KEYS:
                    errors.append(f'unknown field "{key}" in quality (how to fix: remove or move to allowed location)')
            if "fail_on_empty_series" in quality and not isinstance(quality["fail_on_empty_series"], bool):
                errors.append("quality.fail_on_empty_series: expected a boolean (how to fix: use true or false)")

    return errors


def load_and_validate_spec(path: str | Path) -> dict[str, Any]:
    """Load + validate a spec.json with the D-08 contract.

    On any violation: prints the full aggregated list to stderr and raises
    SystemExit(2) — the model-readable "spec problem, model fixes" signal
    (RUN-01, Phase 7). Returns the dict when valid.
    """
    spec = load_spec(path)
    errors = validate_spec(spec)
    if errors:
        print(f"spec.json validation failed ({len(errors)} errors):", file=sys.stderr)
        for err in errors:
            print(f"- {err}", file=sys.stderr)
        raise SystemExit(2)
    return spec


def cache_key_for_url(url: str) -> str:
    """sha256 of the full URL — the .cache/ key design (AGENTS.md)."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def cache_path_for_key(key: str) -> Path:
    """Resolve a cache key to a JSON file path under CACHE_DIR (never outside)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{key}.json"


def throttle(seconds: float = 1.0, last: list[float] | None = None) -> None:
    """Sleep so consecutive calls are at least `seconds` apart (~1 req/s policy).

    The mutable state defaults to the module-level shared list when None
    (never a mutable default argument); a caller may inject its own list for
    per-client pacing.
    """
    state = last if last is not None else _THROTTLE_LAST
    wait = seconds - (time.monotonic() - state[0])
    if wait > 0:
        time.sleep(wait)
    state[0] = time.monotonic()