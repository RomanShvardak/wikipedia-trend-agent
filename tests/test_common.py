"""Shared-infra behavior pins: UA fail-fast, sha256 cache keys, throttle, JSON I/O roundtrip.

Pins the D-04 User-Agent fail-fast contract, the sha256 full-URL cache-key
design, the ~1 req/s throttle, UTF-8 JSON I/O, and logging setup. No network
access; no matplotlib import. monkeypatch + tmp_path only.
"""
import io
import json
import logging
import re
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import format_datetime
from pathlib import Path

import pytest

import common
from common import cache_key_for_url, cache_path_for_key, dump_json, setup_logging, throttle, user_agent

DESCRIPTIVE_UA = "wikipedia-trend-agent/0.1.0 (me@example.com) python-urllib"


def test_user_agent_failfast_default(monkeypatch):
    """The shipped DEFAULT_UA placeholder must trip the fail-fast (D-04)."""
    monkeypatch.delenv("WTI_USER_AGENT", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        user_agent()
    msg = str(excinfo.value)
    assert "placeholder" in msg
    assert "WTI_USER_AGENT" in msg


def test_user_agent_failfast_empty(monkeypatch):
    """Empty and whitespace-only UA values trip the fail-fast."""
    monkeypatch.setenv("WTI_USER_AGENT", "")
    with pytest.raises(SystemExit):
        user_agent()
    monkeypatch.setenv("WTI_USER_AGENT", "   ")
    with pytest.raises(SystemExit):
        user_agent()


def test_user_agent_ok(monkeypatch):
    """A descriptive env value passes through unchanged."""
    monkeypatch.setenv("WTI_USER_AGENT", DESCRIPTIVE_UA)
    assert user_agent() == DESCRIPTIVE_UA


def test_cache_key_deterministic():
    """sha256 of the same URL is stable, 64 chars, hex-only."""
    key = cache_key_for_url("https://example.com/a")
    assert key == cache_key_for_url("https://example.com/a")
    assert len(key) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", key)


def test_cache_key_distinct():
    """Different URLs (even one query param apart) produce different keys."""
    assert cache_key_for_url("https://example.com/a?x=1") != cache_key_for_url("https://example.com/a?x=2")


def test_cache_path_under_cache_dir(tmp_path, monkeypatch):
    """Writes resolve inside the (monkeypatched) CACHE_DIR — never the repo root."""
    monkeypatch.setattr(common, "CACHE_DIR", tmp_path / "cache")
    key = "abc" * 21 + "de"
    expected = tmp_path / "cache" / f"{key}.json"
    assert cache_path_for_key(key) == expected
    assert (tmp_path / "cache").exists()


def test_dump_json_roundtrip_utf8(tmp_path):
    """ensure_ascii=False keeps the literal Cyrillic; roundtrip recovers the same dict."""
    obj = {"label": "Польська: інтервальне голодування", "n": 1.5}
    out = tmp_path / "out" / "m.json"
    dump_json(obj, out)
    raw = out.read_text(encoding="utf-8")
    assert json.loads(raw) == obj
    assert "Польська: інтервальне голодування" in raw


def test_dump_json_creates_parents(tmp_path):
    """Nested parents are created on demand."""
    target = tmp_path / "a" / "b" / "c.json"
    dump_json({"a": 1}, target)
    assert target.exists()


def test_throttle_first_call_immediate():
    """First call with no recorded last time returns immediately (no stale sleep)."""
    start = time.monotonic()
    throttle(0.2)
    assert time.monotonic() - start < 0.15


def test_throttle_second_call_waits():
    """Back-to-back calls are spaced at least `seconds` apart (lenient bound)."""
    start = time.monotonic()
    throttle(0.2)
    throttle(0.2)
    assert time.monotonic() - start >= 0.2


def test_setup_logging_verbose():
    """verbose=True sets DEBUG on the root logger."""
    setup_logging(verbose=True)
    assert logging.getLogger().level == logging.DEBUG


class RecordingStream:
    def __init__(self, body: bytes, *, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.body = body
        self.status = status
        self.headers = headers or {}
        self.reads: list[int | None] = []

    def __enter__(self) -> "RecordingStream":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        self.reads.append(size)
        return self.body if size < 0 else self.body[:size]


def test_common_exports_complete_generic_transport_and_cache_surface():
    required = {
        "ResponseTooLarge",
        "default_transport",
        "header_value",
        "retry_after_seconds",
        "is_retryable_status",
        "read_json_cache",
        "write_json_cache",
    }

    assert required <= set(dir(common))


def test_bounded_transport_rejects_declared_oversize_before_read(monkeypatch):
    stream = RecordingStream(b"12345", headers={"Content-Length": "5"})
    opened = []

    def fake_urlopen(request, timeout):
        opened.append((request, timeout))
        return stream

    monkeypatch.setattr(common.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(common.ResponseTooLarge):
        common.default_transport("https://example.test/data", {}, 3.0, max_bytes=4)

    assert opened
    assert stream.reads == []


@pytest.mark.parametrize("http_error", [False, True])
def test_bounded_transport_reads_limit_plus_one_for_normal_and_error(monkeypatch, http_error):
    stream = RecordingStream(b"12345")
    opened = []

    def fake_urlopen(request, timeout):
        opened.append((request, timeout))
        if http_error:
            raise urllib.error.HTTPError(
                url=request.full_url,
                code=404,
                msg="Not Found",
                hdrs=Message(),
                fp=io.BytesIO(stream.body),
            )
        return stream

    error_reads = []

    class ErrorStream(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            error_reads.append(size)
            return super().read(size)

    def error_urlopen(request, timeout):
        opened.append((request, timeout))
        error = urllib.error.HTTPError(
            url=request.full_url,
            code=404,
            msg="Not Found",
            hdrs=Message(),
            fp=ErrorStream(stream.body),
        )
        raise error

    monkeypatch.setattr(common.urllib.request, "urlopen", error_urlopen if http_error else fake_urlopen)

    with pytest.raises(common.ResponseTooLarge):
        common.default_transport("https://example.test/data", {}, 3.0, max_bytes=4)

    assert opened
    assert (error_reads if http_error else stream.reads) == [5]


def test_bounded_transport_returns_body_at_limit(monkeypatch):
    stream = RecordingStream(b"1234")

    monkeypatch.setattr(common.urllib.request, "urlopen", lambda _request, timeout: stream)

    response = common.default_transport("https://example.test/data", {}, 3.0, max_bytes=4)

    assert response.body == b"1234"
    assert stream.reads == [5]


@pytest.mark.parametrize("max_bytes", [0, -1, 1.5, True])
def test_bounded_transport_rejects_invalid_limit_before_open(monkeypatch, max_bytes):
    def forbidden_urlopen(*_args, **_kwargs):
        raise AssertionError("request must not be opened")

    monkeypatch.setattr(common.urllib.request, "urlopen", forbidden_urlopen)

    with pytest.raises((TypeError, ValueError)):
        common.default_transport("https://example.test/data", {}, max_bytes=max_bytes)


def test_header_lookup_and_retry_helpers_are_case_insensitive_and_bounded():
    now = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)

    assert common.header_value({"rEtRy-AfTeR": "7"}, "Retry-After") == "7"
    assert common.retry_after_seconds({"retry-after": "7"}, now) == 7.0
    http_date = format_datetime(now + timedelta(seconds=9), usegmt=True)
    assert common.retry_after_seconds({"Retry-After": http_date}, now) == 9.0
    for headers in ({}, {"Retry-After": "bad"}, {"Retry-After": "0"}, {"Retry-After": "-2"}):
        assert common.retry_after_seconds(headers, now) == 5.0
    assert common.is_retryable_status(429)
    assert common.is_retryable_status(500)
    assert common.is_retryable_status(599)
    assert not common.is_retryable_status(200)
    assert not common.is_retryable_status(403)
    assert not common.is_retryable_status(404)


def _accept_items(payload: dict[str, object]) -> dict[str, object] | None:
    items = payload.get("items")
    return payload if isinstance(items, list) else None


def test_json_cache_valid_hit_uses_full_url_keys_and_aware_timestamp(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "CACHE_DIR", tmp_path / "cache")
    url = "https://example.test/api?project=uk&query=%D0%BA%D0%BE%D1%82"
    other_url = url + "&limit=5"
    payload = {"query": "кот", "items": []}

    path = common.write_json_cache(url, payload)
    other_path = common.write_json_cache(other_url, {"query": "кішка", "items": [1]})

    assert common.read_json_cache(url, 24.0, _accept_items) == payload
    assert common.read_json_cache(other_url, 24.0, _accept_items) != payload
    assert path != other_path
    envelope = json.loads(path.read_text(encoding="utf-8"))
    assert set(envelope) == {"fetched_at", "status", "response"}
    assert envelope["status"] == 200
    assert envelope["response"] == payload
    assert datetime.fromisoformat(envelope["fetched_at"]).tzinfo is not None


def test_json_cache_ttl_zero_and_stale_entries_miss_safely(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "CACHE_DIR", tmp_path / "cache")
    url = "https://example.test/api?x=1"
    path = common.write_json_cache(url, {"items": []})
    stale_path = common.write_json_cache(
        "https://example.test/api?x=2",
        {"items": []},
        fetched_at=datetime.now(timezone.utc) - timedelta(days=2),
    )

    assert common.read_json_cache(url, 0.0, _accept_items) is None
    assert common.read_json_cache("https://example.test/api?x=2", 1.0, _accept_items) is None
    assert path.exists()
    assert stale_path.exists()


@pytest.mark.parametrize("raw", ["not json", "null", "{}", '{"status":200,"response":null}'])
def test_json_cache_removes_corrupt_empty_or_null_entries(tmp_path, monkeypatch, raw):
    monkeypatch.setattr(common, "CACHE_DIR", tmp_path / "cache")
    url = f"https://example.test/api?x={raw}"
    path = common.cache_path_for_key(common.cache_key_for_url(url))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")

    assert common.read_json_cache(url, 24.0, _accept_items) is None
    assert not path.exists()


def test_json_cache_validator_rejection_removes_http_200_error_envelope(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "CACHE_DIR", tmp_path / "cache")
    url = "https://example.test/api?error=1"
    path = common.write_json_cache(
        url,
        {"error": {"code": "search-title-disabled", "info": "disabled"}},
    )

    assert common.read_json_cache(url, 24.0, _accept_items) is None
    assert not path.exists()
