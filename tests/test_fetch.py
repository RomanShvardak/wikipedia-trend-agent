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
from datetime import date, datetime, timedelta, timezone
from email.message import Message
from email.utils import format_datetime
from pathlib import Path

import pytest

import common
import fetch_pageviews
from fetch_pageviews import classify_404, default_transport, main, series_url

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
    monkeypatch.setattr(fetch_pageviews, "sleep", lambda _seconds: None)
    return throttle_calls


def _args(spec_path: Path, out_path: Path) -> list[str]:
    return ["--spec", str(spec_path), "--out", str(out_path)]


def _series_urls(spec_path: Path) -> list[str]:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    window = spec["window"]
    return [
        series_url(series["project"], series["article"], window["start"], window["end"])
        for series in spec["series"]
    ]


def _write_cache(spec_path: Path, response: object, fetched_at: datetime) -> Path:
    url = _series_urls(spec_path)[0]
    path = common.cache_path_for_key(common.cache_key_for_url(url))
    common.dump_json(
        {"fetched_at": fetched_at.isoformat(), "status": 200, "response": response},
        path,
    )
    return path


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

    response = default_transport(
        "https://wikimedia.org/example",
        {"User-Agent": DESCRIPTIVE_UA},
        3.0,
    )

    assert response.status == 404
    assert response.body == body


def test_stale_cache_envelope_refetches_and_is_rewritten(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
):
    _configure(monkeypatch, tmp_path)
    response = json.loads(_response_body("pageviews.200.json"))
    stale_at = datetime.now(timezone.utc) - timedelta(days=2)
    stale_path = _write_cache(spec_example_path, response, stale_at)
    second_url = _series_urls(spec_example_path)[1]
    second_path = common.cache_path_for_key(common.cache_key_for_url(second_url))
    common.dump_json(
        {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "status": 200,
            "response": response,
        },
        second_path,
    )
    stub = transport_stub([(200, {}, _response_body("pageviews.200.json"))])

    assert main(_args(spec_example_path, tmp_path / "out"), transport=stub) == 0

    assert len(stub.calls) == 1
    rewritten = json.loads(stale_path.read_text(encoding="utf-8"))
    assert datetime.fromisoformat(rewritten["fetched_at"]) > stale_at
    assert rewritten["status"] == 200
    assert isinstance(rewritten["response"]["items"], list)


def test_ttl_hours_zero_forces_cache_refetch(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
):
    _configure(monkeypatch, tmp_path)
    first_stub = transport_stub([(200, {}, _response_body("pageviews.200.json"))] * 2)
    assert main(_args(spec_example_path, tmp_path / "out"), transport=first_stub) == 0
    second_stub = transport_stub([(200, {}, _response_body("pageviews.200.json"))] * 2)
    args = [*_args(spec_example_path, tmp_path / "out"), "--ttl-hours", "0"]

    assert main(args, transport=second_stub) == 0

    assert len(second_stub.calls) == 2


def test_ttl_hours_environment_default_forces_cache_refetch(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
):
    _configure(monkeypatch, tmp_path)
    first_stub = transport_stub([(200, {}, _response_body("pageviews.200.json"))] * 2)
    assert main(_args(spec_example_path, tmp_path / "out"), transport=first_stub) == 0
    monkeypatch.setenv("WTI_TTL_HOURS", "0")
    second_stub = transport_stub([(200, {}, _response_body("pageviews.200.json"))] * 2)

    assert main(_args(spec_example_path, tmp_path / "out"), transport=second_stub) == 0

    assert len(second_stub.calls) == 2


@pytest.mark.parametrize("failure_status", [404, 503])
def test_non_200_never_writes_cache_and_later_200_refetches(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
    failure_status,
):
    _configure(monkeypatch, tmp_path)
    if failure_status == 404:
        first_stub = transport_stub([(404, {}, b"failure"), (200, {}, _response_body("pageviews.200.json")), (200, {}, _response_body("pageviews.200.json"))])
        expected_first_calls = 1
    else:
        first_stub = transport_stub([(503, {}, b"failure")] * 6)
        expected_first_calls = 6
    out_dir = tmp_path / "out"

    assert main(_args(spec_example_path, out_dir), transport=first_stub) == 1
    assert len(first_stub.calls) == expected_first_calls
    assert list((tmp_path / "cache").glob("*.json")) == []
    assert not (out_dir / "series.csv").exists()

    second_stub = transport_stub([(200, {}, _response_body("pageviews.200.json"))] * 2)
    assert main(_args(spec_example_path, out_dir), transport=second_stub) == 0
    assert len(second_stub.calls) == 2
    assert len(list((tmp_path / "cache").glob("*.json"))) == 2


def test_corrupt_cache_warns_drops_entry_and_refetches(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
    capsys,
):
    _configure(monkeypatch, tmp_path)
    corrupt_path = _write_cache(
        spec_example_path,
        {"unexpected": True},
        datetime.now(timezone.utc),
    )
    stub = transport_stub([(200, {}, _response_body("pageviews.200.json"))] * 2)

    assert main(_args(spec_example_path, tmp_path / "out"), transport=stub) == 0

    warning = "pl-post-przerywany: corrupt cache entry; refetching from network"
    assert warning in capsys.readouterr().err
    assert len(stub.calls) == 2
    rewritten = json.loads(corrupt_path.read_text(encoding="utf-8"))
    assert isinstance(rewritten["response"]["items"], list)


def test_malformed_200_is_reported_and_not_cached(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
    capsys,
):
    _configure(monkeypatch, tmp_path)
    stub = transport_stub([(200, {}, b'{"unexpected": []}')])
    out_dir = tmp_path / "out"

    assert main(_args(spec_example_path, out_dir), transport=stub) == 1

    assert "items list" in capsys.readouterr().err
    assert list((tmp_path / "cache").glob("*.json")) == []
    assert not (out_dir / "series.csv").exists()


def test_cached_item_with_invalid_types_never_reaches_csv(
    spec_example_path,
    tmp_path,
    monkeypatch,
    capsys,
):
    _configure(monkeypatch, tmp_path)
    invalid_response = {
        "items": [
            {
                "project": "pl.wikipedia",
                "article": "Warszawa",
                "granularity": "daily",
                "timestamp": "20260923",
                "access": "all-access",
                "agent": "user",
                "views": 1.5,
            }
        ]
    }
    _write_cache(spec_example_path, invalid_response, datetime.now(timezone.utc))
    out_dir = tmp_path / "out"

    def forbidden_transport(url, headers, timeout=30.0):
        raise AssertionError("valid-looking cache should not reach transport")

    assert main(_args(spec_example_path, out_dir), transport=forbidden_transport) == 1

    assert "invalid timestamp" in capsys.readouterr().err
    assert not (out_dir / "series.csv").exists()


def _write_404_spec(
    source_spec: Path,
    tmp_path: Path,
    end_date: date,
    *,
    fail_on_empty_series: bool,
) -> Path:
    spec = copy.deepcopy(json.loads(source_spec.read_text(encoding="utf-8")))
    start_date = end_date - timedelta(days=2)
    spec["window"]["start"] = start_date.strftime("%Y%m%d")
    spec["window"]["end"] = end_date.strftime("%Y%m%d")
    spec["quality"] = {"fail_on_empty_series": fail_on_empty_series}
    spec_path = tmp_path / "spec-404.json"
    tmp_path.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    return spec_path


@pytest.mark.parametrize(
    ("days_ago", "expected"),
    [(0, "not_loaded"), (2, "not_loaded"), (3, "no_views")],
)
def test_classify_404_uses_inclusive_utc_recency_gap(days_ago, expected):
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    window_end = yesterday - timedelta(days=days_ago)

    assert classify_404(window_end.strftime("%Y%m%d"), yesterday) == expected
    assert _response_body("pageviews.404.json")


def test_not_loaded_404_writes_nothing_and_continues(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
    capsys,
):
    _configure(monkeypatch, tmp_path)
    spec_path = _write_404_spec(
        spec_example_path,
        tmp_path,
        datetime.now(timezone.utc) - timedelta(days=1),
        fail_on_empty_series=False,
    )
    stub = transport_stub(
        [
            (404, {}, _response_body("pageviews.404.json")),
            (200, {}, _response_body("pageviews.200.json")),
        ]
    )

    assert main(_args(spec_path, tmp_path / "out"), transport=stub) == 0

    rows = list(csv.DictReader((tmp_path / "out" / "series.csv").open(encoding="utf-8")))
    assert [row["series_id"] for row in rows] == ["cs-pust-prerusovany"]
    assert "pl-post-przerywany: data not yet loaded — retry later" in capsys.readouterr().err


def test_no_views_404_zero_fills_the_requested_window(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
    capsys,
):
    _configure(monkeypatch, tmp_path)
    end_date = datetime.now(timezone.utc).date() - timedelta(days=4)
    spec_path = _write_404_spec(
        spec_example_path,
        tmp_path,
        end_date,
        fail_on_empty_series=False,
    )
    stub = transport_stub([(404, {}, _response_body("pageviews.404.json"))] * 2)

    assert main(_args(spec_path, tmp_path / "out"), transport=stub) == 0

    with (tmp_path / "out" / "series.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    expected_dates = [(end_date - timedelta(days=offset)).strftime("%Y-%m-%d") for offset in (2, 1, 0)]
    assert [row["date"] for row in rows] == expected_dates + expected_dates
    assert {row["views"] for row in rows} == {"0"}
    assert capsys.readouterr().err.count("no views for the requested window") == 2


def test_fail_on_empty_series_controls_404_fatal_exit(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
    capsys,
):
    _configure(monkeypatch, tmp_path)
    end_date = datetime.now(timezone.utc).date() - timedelta(days=4)

    fatal_spec = _write_404_spec(
        spec_example_path,
        tmp_path / "fatal",
        end_date,
        fail_on_empty_series=True,
    )
    fatal_stub = transport_stub([(404, {}, _response_body("pageviews.404.json"))] * 2)
    assert main(_args(fatal_spec, tmp_path / "fatal" / "out"), transport=fatal_stub) == 1
    assert "no views for the requested window" in capsys.readouterr().err
    assert not (tmp_path / "fatal" / "out" / "series.csv").exists()

    permissive_spec = _write_404_spec(
        spec_example_path,
        tmp_path / "permissive",
        end_date,
        fail_on_empty_series=False,
    )
    permissive_stub = transport_stub([(404, {}, _response_body("pageviews.404.json"))] * 2)
    assert main(_args(permissive_spec, tmp_path / "permissive" / "out"), transport=permissive_stub) == 0
    assert "no views for the requested window" in capsys.readouterr().err
    assert (tmp_path / "permissive" / "out" / "series.csv").exists()


def test_retry_after_integer_seconds_is_honored(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
):
    _configure(monkeypatch, tmp_path)
    sleeps: list[float] = []
    monkeypatch.setattr(fetch_pageviews, "sleep", sleeps.append)
    stub = transport_stub(
        [
            (429, {"Retry-After": "3"}, b"rate limited"),
            (200, {}, _response_body("pageviews.200.json")),
            (429, {"Retry-After": "3"}, b"rate limited"),
            (200, {}, _response_body("pageviews.200.json")),
        ]
    )

    assert main(_args(spec_example_path, tmp_path / "out"), transport=stub) == 0
    assert sleeps == [3.0, 3.0]
    assert len(stub.calls) == 4


def test_retry_after_http_date_is_parsed(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
):
    _configure(monkeypatch, tmp_path)
    sleeps: list[float] = []
    monkeypatch.setattr(fetch_pageviews, "sleep", sleeps.append)
    retry_at = datetime.now(timezone.utc) + timedelta(seconds=8)
    stub = transport_stub(
        [
            (429, {"Retry-After": format_datetime(retry_at, usegmt=True)}, b"rate limited"),
            (200, {}, _response_body("pageviews.200.json")),
            (429, {"Retry-After": format_datetime(retry_at, usegmt=True)}, b"rate limited"),
            (200, {}, _response_body("pageviews.200.json")),
        ]
    )

    assert main(_args(spec_example_path, tmp_path / "out"), transport=stub) == 0
    assert len(sleeps) == 2
    assert all(seconds >= 5 for seconds in sleeps)


def test_retry_after_missing_uses_five_second_fallback(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
):
    _configure(monkeypatch, tmp_path)
    sleeps: list[float] = []
    monkeypatch.setattr(fetch_pageviews, "sleep", sleeps.append)
    stub = transport_stub(
        [
            (429, {}, b"rate limited"),
            (200, {}, _response_body("pageviews.200.json")),
            (429, {}, b"rate limited"),
            (200, {}, _response_body("pageviews.200.json")),
        ]
    )

    assert main(_args(spec_example_path, tmp_path / "out"), transport=stub) == 0
    assert sleeps == [5.0, 5.0]


def test_retry_cap_marks_series_failed_and_continues(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(fetch_pageviews, "sleep", lambda _seconds: None)
    stub = transport_stub(
        [
            (429, {}, b"rate limited"),
            (429, {}, b"rate limited"),
            (429, {}, b"rate limited"),
            (200, {}, _response_body("pageviews.200.json")),
        ]
    )

    assert main(_args(spec_example_path, tmp_path / "out"), transport=stub) == 3
    assert len(stub.calls) == 4
    assert (tmp_path / "out" / "series.csv").exists()
    assert list((tmp_path / "cache").glob("*.json"))


def test_all_series_fail_returns_one_without_cache(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(fetch_pageviews, "sleep", lambda _seconds: None)
    stub = transport_stub([(429, {}, b"rate limited")] * 6)

    assert main(_args(spec_example_path, tmp_path / "out"), transport=stub) == 1
    assert len(stub.calls) == 6
    assert not (tmp_path / "out" / "series.csv").exists()
    assert list((tmp_path / "cache").glob("*.json")) == []


def test_403_aborts_immediately_without_retry_or_cache(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
    capsys,
):
    _configure(monkeypatch, tmp_path)
    stub = transport_stub([(403, {}, b"forbidden"), (200, {}, _response_body("pageviews.200.json"))])

    assert main(_args(spec_example_path, tmp_path / "out"), transport=stub) == 1

    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 1
    assert "pl-post-przerywany: HTTP 403 — set a real WTI_USER_AGENT contact and retry" in captured.err
    assert len(stub.calls) == 1
    assert not (tmp_path / "out" / "series.csv").exists()
    assert list((tmp_path / "cache").glob("*.json")) == []


def test_stream_contract_keeps_diagnostics_off_stdout(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
    capsys,
):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(fetch_pageviews, "sleep", lambda _seconds: None)
    stub = transport_stub(
        [
            (429, {}, b"rate limited"),
            (429, {}, b"rate limited"),
            (429, {}, b"rate limited"),
            (200, {}, _response_body("pageviews.200.json")),
        ]
    )

    assert main(_args(spec_example_path, tmp_path / "out"), transport=stub) == 3

    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 1
    assert "series=2" not in captured.out
    diagnostic_lines = captured.err.splitlines()
    assert diagnostic_lines
    assert all(line.startswith(("pl-post-przerywany: ", "cs-pust-prerusovany: ")) for line in diagnostic_lines)
    assert "rate limited" not in " ".join(diagnostic_lines)
