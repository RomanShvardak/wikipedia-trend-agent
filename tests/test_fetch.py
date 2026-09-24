"""Fetch CLI contract tests using an injected transport and committed fixtures.

Every end-to-end test is zero-network: the stub returns captured bodies, cache
state lives under tmp_path, and the production urllib wrapper is exercised only
with a monkeypatched urlopen.
"""
import copy
import csv
import io
import json
import time
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path

import pytest

import common
import fetch_pageviews
from fetch_pageviews import default_transport, main, series_url

DESCRIPTIVE_UA = "wikipedia-trend-agent/0.1.0 (maintainer@invalid.example.net) python-urllib"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _response_body(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


def _configure(monkeypatch, tmp_path: Path) -> list[tuple[float, list[float]]]:
    """Set UA, cache, and no-wait throttle; return a throttle call log."""
    throttle_calls: list[tuple[float, list[float]]] = []
    monkeypatch.setenv("WTI_USER_AGENT", DESCRIPTIVE_UA)
    monkeypatch.setattr(common, "CACHE_DIR", tmp_path / "cache")

    def record_throttle(seconds: float, last: list[float]) -> None:
        throttle_calls.append((seconds, last))
        last[0] = time.monotonic()

    monkeypatch.setattr(common, "throttle", record_throttle)
    return throttle_calls


def _args(spec_path: Path, out_path: Path) -> list[str]:
    return ["--spec", str(spec_path), "--out", str(out_path)]


def test_valid_spec_writes_exact_deterministic_csv(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
    capsys,
):
    throttle_calls = _configure(monkeypatch, tmp_path)
    stub = transport_stub([(200, {}, _response_body("pageviews.200.json"))] * 2)
    out_dir = tmp_path / "out"

    assert main(_args(spec_example_path, out_dir), transport=stub) == 0

    output = out_dir / "series.csv"
    with output.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == ["date", "views", "series_id", "project", "article"]
    assert [row[2:] for row in rows[1:]] == [
        ["cs-pust-prerusovany", "cs.wikipedia", "P%C5%AFst_p%C5%99eru%C5%A1ovan%C3%BD"],
        ["pl-post-przerywany", "pl.wikipedia", "Post_przerywany"],
    ]
    assert [(row[0], row[1]) for row in rows[1:]] == [("2026-09-23", "1868")] * 2
    assert len(stub.calls) == 2
    assert len(throttle_calls) == 2
    stdout = capsys.readouterr().out
    assert len(stdout.splitlines()) == 1
    assert "Fetched 2 row(s) for 2 series" in stdout


def test_url_builder_uses_ten_digit_ten_hour_timestamps():
    url = series_url("pl.wikipedia", "Warszawa", "20240923", "20260923")

    assert "daily/2024092300/2026092300" in url
    assert "/Warszawa/" in url


def test_main_builds_url_from_each_verbatim_series(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
):
    _configure(monkeypatch, tmp_path)
    stub = transport_stub([(200, {}, _response_body("pageviews.200.json"))] * 2)

    assert main(_args(spec_example_path, tmp_path / "out"), transport=stub) == 0

    urls = [call[0] for call in stub.calls]
    assert all("daily/2024092300/2026092000" in url for url in urls)
    assert "/Post_przerywany/" in urls[0]
    assert "/P%C5%AFst_p%C5%99eru%C5%A1ovan%C3%BD/" in urls[1]


def test_request_carries_descriptive_user_agent(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
):
    _configure(monkeypatch, tmp_path)
    stub = transport_stub([(200, {}, _response_body("pageviews.200.json"))] * 2)

    assert main(_args(spec_example_path, tmp_path / "out"), transport=stub) == 0

    assert all(call_headers["User-Agent"] == DESCRIPTIVE_UA for _, call_headers in stub.calls)
    assert all(call_headers["Accept"] == "application/json" for _, call_headers in stub.calls)


def test_invalid_spec_exits_two_without_partial_csv(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
    capsys,
):
    _configure(monkeypatch, tmp_path)
    spec = copy.deepcopy(json.loads(spec_example_path.read_text(encoding="utf-8")))
    spec.pop("request")
    bad_spec = tmp_path / "bad.json"
    bad_spec.write_text(json.dumps(spec), encoding="utf-8")
    out_dir = tmp_path / "out"
    stub = transport_stub([])

    with pytest.raises(SystemExit) as excinfo:
        main(_args(bad_spec, out_dir), transport=stub)

    assert excinfo.value.code == 2
    assert "spec.json validation failed" in capsys.readouterr().err
    assert not (out_dir / "series.csv").exists()
    assert stub.calls == []


def test_missing_user_agent_aborts_before_transport(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
):
    monkeypatch.delenv("WTI_USER_AGENT", raising=False)
    monkeypatch.setattr(common, "CACHE_DIR", tmp_path / "cache")
    stub = transport_stub([])

    with pytest.raises(SystemExit) as excinfo:
        main(_args(spec_example_path, tmp_path / "out"), transport=stub)

    assert "WTI_USER_AGENT" in str(excinfo.value)
    assert stub.calls == []
    assert not (tmp_path / "out" / "series.csv").exists()


def test_second_run_is_cache_hit_and_byte_deterministic(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
    capsys,
):
    _configure(monkeypatch, tmp_path)
    out_dir = tmp_path / "out"
    first_stub = transport_stub([(200, {}, _response_body("pageviews.200.json"))] * 2)
    assert main(_args(spec_example_path, out_dir), transport=first_stub) == 0
    first_bytes = (out_dir / "series.csv").read_bytes()
    first_stdout = capsys.readouterr().out
    second_stub = transport_stub([])

    assert main(_args(spec_example_path, out_dir), transport=second_stub) == 0

    assert second_stub.calls == []
    assert (out_dir / "series.csv").read_bytes() == first_bytes
    assert "cache hits: 2/2" in capsys.readouterr().out
    assert "cache hits: 0/2" in first_stdout


def test_distinct_series_urls_create_distinct_cache_files(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
):
    _configure(monkeypatch, tmp_path)
    stub = transport_stub([(200, {}, _response_body("pageviews.200.json"))] * 2)

    assert main(_args(spec_example_path, tmp_path / "out"), transport=stub) == 0

    cache_files = list((tmp_path / "cache").glob("*.json"))
    assert len(cache_files) == 2
    envelopes = [json.loads(path.read_text(encoding="utf-8")) for path in cache_files]
    assert all(envelope["status"] == 200 for envelope in envelopes)
    assert all("fetched_at" in envelope for envelope in envelopes)
    assert all("items" in envelope["response"] for envelope in envelopes)


def test_default_transport_converts_http_error_to_response(monkeypatch):
    body = b'{"status":404}'
    error = urllib.error.HTTPError(
        url="https://wikimedia.org/example",
        code=404,
        msg="Not Found",
        hdrs=Message(),
        fp=io.BytesIO(body),
    )

    def fail_urlopen(request, timeout):
        assert isinstance(request, urllib.request.Request)
        assert timeout == 3.0
        raise error

    monkeypatch.setattr(fetch_pageviews.urllib.request, "urlopen", fail_urlopen)

    response = default_transport("https://wikimedia.org/example", {"User-Agent": DESCRIPTIVE_UA}, 3.0)

    assert response.status == 404
    assert response.body == body
