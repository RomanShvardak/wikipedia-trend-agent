"""Shared-infra behavior pins: UA fail-fast, sha256 cache keys, throttle, JSON I/O roundtrip.

Pins the D-04 User-Agent fail-fast contract, the sha256 full-URL cache-key
design, the ~1 req/s throttle, UTF-8 JSON I/O, and logging setup. No network
access; no matplotlib import. monkeypatch + tmp_path only.
"""
import json
import logging
import re
import time
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