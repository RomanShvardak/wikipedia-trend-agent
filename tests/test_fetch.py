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
from fetch_pageviews import (
    chunk_ranges,
    classify_404,
    default_transport,
    effective_end,
    main,
    series_url,
)

DESCRIPTIVE_UA = "wikipedia-trend-agent/0.1.0 (maintainer@invalid.example.net) python-urllib"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _response_body(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


def _chunk_aware_body(url: str, body: bytes) -> bytes:
    payload = json.loads(body.decode("utf-8"))
    start_ymd = url.split("/daily/", 1)[1].split("/", 1)[0][:8]
    for item in payload.get("items", []):
        if isinstance(item, dict) and isinstance(item.get("timestamp"), str):
            item["timestamp"] = f"{start_ymd}00"
    return json.dumps(payload).encode("utf-8")


def _chunk_aware_transport(transport):
    def fetch(url, headers, timeout=30.0):
        response = transport(url, headers, timeout)
        if response.status != 200:
            return response
        return fetch_pageviews.TransportResponse(
            status=response.status,
            headers=response.headers,
            body=_chunk_aware_body(url, response.body),
        )

    setattr(fetch, "calls", transport.calls)
    return fetch


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
        series_url(series["project"], series["article"], chunk_start, chunk_end)
        for series in spec["series"]
        for chunk_start, chunk_end in chunk_ranges(window["start"], window["end"])
    ]


def _write_cache(
    spec_path: Path,
    response: object,
    fetched_at: datetime,
    *,
    chunk_aware: bool = True,
) -> Path:
    url = _series_urls(spec_path)[0]
    path = common.cache_path_for_key(common.cache_key_for_url(url))
    cached_response: object = response
    if chunk_aware:
        cached_response = json.loads(
            _chunk_aware_body(url, json.dumps(response).encode("utf-8")).decode(
                "utf-8"
            )
        )
    common.dump_json(
        {"fetched_at": fetched_at.isoformat(), "status": 200, "response": cached_response},
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
    stub = _chunk_aware_transport(
        transport_stub([(200, {}, _response_body("pageviews.200.json"))] * 4)
    )
    out_dir = tmp_path / "out"

    assert main(_args(spec_example_path, out_dir), transport=stub) == 0

    output = out_dir / "series.csv"
    with output.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == ["date", "views", "series_id", "project", "article"]
    assert [row[2:] for row in rows[1:]] == [
        ["cs-pust-prerusovany", "cs.wikipedia", "P%C5%AFst_p%C5%99eru%C5%A1ovan%C3%BD"],
        ["cs-pust-prerusovany", "cs.wikipedia", "P%C5%AFst_p%C5%99eru%C5%A1ovan%C3%BD"],
        ["pl-post-przerywany", "pl.wikipedia", "Post_przerywany"],
        ["pl-post-przerywany", "pl.wikipedia", "Post_przerywany"],
    ]
    assert [(row[0], row[1]) for row in rows[1:]] == [("2026-09-23", "1868")] * 4
    assert len(stub.calls) == 4
    assert len(throttle_calls) == 4
    stdout = capsys.readouterr().out
    assert len(stdout.splitlines()) == 1
    assert "Fetched 4 row(s) for 2 series" in stdout


def test_url_builder_uses_ten_digit_ten_hour_timestamps():
    url = series_url("pl.wikipedia", "Warszawa", "20240923", "20260923")

    assert "daily/2024092300/2026092300" in url
    assert "/Warszawa/" in url


@pytest.mark.parametrize(
    ("start", "days", "expected"),
    [
        ("20240101", 1, [("20240101", "20240101")]),
        ("20240101", 365, [("20240101", "20241230")]),
        (
            "20240101",
            366,
            [("20240101", "20241230"), ("20241231", "20241231")],
        ),
        (
            "20240101",
            800,
            [
                ("20240101", "20241230"),
                ("20241231", "20251230"),
                ("20251231", "20260310"),
            ],
        ),
    ],
)
def test_chunk_ranges_splits_exact_inclusive_back_from_end(start, days, expected):
    start_date = datetime.strptime(start, "%Y%m%d").date()
    end_date = start_date + timedelta(days=days - 1)

    assert chunk_ranges(start, end_date.strftime("%Y%m%d")) == expected


@pytest.mark.parametrize("days", [1, 365, 366, 800, 801])
def test_chunk_ranges_are_contiguous_and_cover_exact_window(days):
    start = date(2024, 1, 1)
    end = start + timedelta(days=days - 1)

    ranges = chunk_ranges(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
    covered: list[date] = []
    for index, (range_start, range_end) in enumerate(ranges):
        current_start = datetime.strptime(range_start, "%Y%m%d").date()
        current_end = datetime.strptime(range_end, "%Y%m%d").date()
        assert (current_end - current_start).days + 1 <= 365
        if index:
            previous_end = datetime.strptime(ranges[index - 1][1], "%Y%m%d").date()
            assert current_start == previous_end + timedelta(days=1)
        covered.extend(
            previous + timedelta(days=offset)
            for offset in range((current_end - current_start).days + 1)
            for previous in (current_start,)
        )

    assert covered == [start + timedelta(days=offset) for offset in range(days)]


def test_chunk_ranges_rejects_reversed_window():
    with pytest.raises(ValueError, match="window start after end"):
        chunk_ranges("20240102", "20240101")


def test_effective_end_clamps_today_to_yesterday_without_changing_yesterday():
    today = date(2026, 9, 24)
    yesterday = date(2026, 9, 23)

    assert effective_end("20260924", yesterday) == "20260923"
    assert effective_end("20260923", yesterday) == "20260923"
    assert effective_end("20260922", yesterday) == "20260922"
    assert today == date(2026, 9, 24)


def test_main_uses_one_injected_utc_clock_for_clamp_and_404(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
):
    _configure(monkeypatch, tmp_path)
    spec = copy.deepcopy(json.loads(spec_example_path.read_text(encoding="utf-8")))
    spec["window"]["start"] = "20260901"
    spec["window"]["end"] = "20260924"
    spec_path = tmp_path / "today-spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    stub = _chunk_aware_transport(
        transport_stub([(200, {}, _response_body("pageviews.200.json"))] * 4)
    )

    assert main(
        _args(spec_path, tmp_path / "out"),
        transport=stub,
        today_utc=date(2026, 9, 24),
    ) == 0

    assert all("daily/2026090100/2026092300" in call[0] for call in stub.calls)
    assert classify_404("20260923", date(2026, 9, 24) - timedelta(days=1)) == "not_loaded"


def test_200_response_holes_are_absent_from_series_csv(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
):
    _configure(monkeypatch, tmp_path)
    spec = copy.deepcopy(json.loads(spec_example_path.read_text(encoding="utf-8")))
    spec["series"] = spec["series"][:1]
    spec["window"]["start"] = "20240901"
    spec["window"]["end"] = "20240903"
    spec_path = tmp_path / "holes-spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    response = {
        "items": [
            {"timestamp": "2024090100", "views": 11},
            {"timestamp": "2024090300", "views": 13},
        ]
    }
    body = json.dumps(response).encode("utf-8")
    stub = transport_stub([(200, {}, body)])

    assert main(_args(spec_path, tmp_path / "out"), transport=stub) == 0

    with (tmp_path / "out" / "series.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["date"] for row in rows] == [
        "2024-09-01",
        "2024-09-03",
    ]
    assert "2024-09-02" not in {row["date"] for row in rows}
    assert all(row["views"] != "0" for row in rows)


def test_multi_chunk_run_uses_distinct_urls_and_cache_files(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
):
    _configure(monkeypatch, tmp_path)
    spec = copy.deepcopy(json.loads(spec_example_path.read_text(encoding="utf-8")))
    spec["series"] = spec["series"][:1]
    spec["window"]["start"] = "20240102"
    spec["window"]["end"] = "20251231"
    spec_path = tmp_path / "multi-chunk-spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    stub = _chunk_aware_transport(
        transport_stub([(200, {}, _response_body("pageviews.200.json"))] * 2)
    )

    assert main(
        _args(spec_path, tmp_path / "out"),
        transport=stub,
        today_utc=date(2026, 9, 24),
    ) == 0

    assert len(stub.calls) == 2
    assert stub.calls[0][0] != stub.calls[1][0]
    assert stub.calls[0][0].split("/daily/")[1] != stub.calls[1][0].split("/daily/")[1]
    assert len(list((tmp_path / "cache").glob("*.json"))) == 2


def test_main_builds_url_from_each_verbatim_series(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
):
    _configure(monkeypatch, tmp_path)
    stub = _chunk_aware_transport(
        transport_stub([(200, {}, _response_body("pageviews.200.json"))] * 4)
    )

    assert main(_args(spec_example_path, tmp_path / "out"), transport=stub) == 0

    urls = [call[0] for call in stub.calls]
    assert any("daily/2024092300/2025092200" in url for url in urls)
    assert any("daily/2025092300/2026092000" in url for url in urls)
    assert all("/Post_przerywany/" in url for url in urls[0:2])
    assert all("/P%C5%AFst_p%C5%99eru%C5%A1ovan%C3%BD/" in url for url in urls[2:4])


def test_request_carries_descriptive_user_agent(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
):
    _configure(monkeypatch, tmp_path)
    stub = _chunk_aware_transport(
        transport_stub([(200, {}, _response_body("pageviews.200.json"))] * 4)
    )

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
    first_stub = _chunk_aware_transport(
        transport_stub([(200, {}, _response_body("pageviews.200.json"))] * 4)
    )
    assert main(_args(spec_example_path, out_dir), transport=first_stub) == 0
    first_bytes = (out_dir / "series.csv").read_bytes()
    first_stdout = capsys.readouterr().out
    second_stub = transport_stub([])

    assert main(_args(spec_example_path, out_dir), transport=second_stub) == 0

    assert second_stub.calls == []
    assert (out_dir / "series.csv").read_bytes() == first_bytes
    assert "cache hits: 4/4" in capsys.readouterr().out
    assert "cache hits: 0/4" in first_stdout


def test_distinct_series_urls_create_distinct_cache_files(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
):
    _configure(monkeypatch, tmp_path)
    stub = _chunk_aware_transport(
        transport_stub([(200, {}, _response_body("pageviews.200.json"))] * 4)
    )

    assert main(_args(spec_example_path, tmp_path / "out"), transport=stub) == 0

    cache_files = list((tmp_path / "cache").glob("*.json"))
    assert len(cache_files) == 4
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
    second_url = _series_urls(spec_example_path)[2]
    second_path = common.cache_path_for_key(common.cache_key_for_url(second_url))
    common.dump_json(
        {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "status": 200,
            "response": response,
        },
        second_path,
    )
    stub = _chunk_aware_transport(
        transport_stub([(200, {}, _response_body("pageviews.200.json"))] * 3)
    )

    assert main(_args(spec_example_path, tmp_path / "out"), transport=stub) == 0

    assert len(stub.calls) == 3
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
    first_stub = _chunk_aware_transport(
        transport_stub([(200, {}, _response_body("pageviews.200.json"))] * 4)
    )
    assert main(_args(spec_example_path, tmp_path / "out"), transport=first_stub) == 0
    second_stub = _chunk_aware_transport(
        transport_stub([(200, {}, _response_body("pageviews.200.json"))] * 4)
    )
    args = [*_args(spec_example_path, tmp_path / "out"), "--ttl-hours", "0"]

    assert main(args, transport=second_stub) == 0

    assert len(second_stub.calls) == 4


def test_ttl_hours_environment_default_forces_cache_refetch(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
):
    _configure(monkeypatch, tmp_path)
    first_stub = _chunk_aware_transport(
        transport_stub([(200, {}, _response_body("pageviews.200.json"))] * 4)
    )
    assert main(_args(spec_example_path, tmp_path / "out"), transport=first_stub) == 0
    monkeypatch.setenv("WTI_TTL_HOURS", "0")
    second_stub = _chunk_aware_transport(
        transport_stub([(200, {}, _response_body("pageviews.200.json"))] * 4)
    )

    assert main(_args(spec_example_path, tmp_path / "out"), transport=second_stub) == 0

    assert len(second_stub.calls) == 4


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
        first_stub = _chunk_aware_transport(
            transport_stub(
                [
                    (404, {}, b"failure"),
                    (200, {}, _response_body("pageviews.200.json")),
                    (404, {}, b"failure"),
                    (200, {}, _response_body("pageviews.200.json")),
                ]
            )
        )
        expected_first_calls = 4
    else:
        first_stub = transport_stub([(503, {}, b"failure")] * 12)
        expected_first_calls = 12
    out_dir = tmp_path / "out"

    expected_first_exit = 0 if failure_status == 404 else 1
    assert main(_args(spec_example_path, out_dir), transport=first_stub) == expected_first_exit
    assert len(first_stub.calls) == expected_first_calls

    if failure_status == 404:
        assert len(list((tmp_path / "cache").glob("*.json"))) == 2
        second_stub = transport_stub(
            [(404, {}, b"failure"), (404, {}, b"failure")]
        )
        assert main(_args(spec_example_path, out_dir), transport=second_stub) == 0
        assert len(second_stub.calls) == 2
    else:
        assert list((tmp_path / "cache").glob("*.json")) == []
        assert not (out_dir / "series.csv").exists()
        second_stub = _chunk_aware_transport(
            transport_stub([(200, {}, _response_body("pageviews.200.json"))] * 4)
        )
        assert main(_args(spec_example_path, out_dir), transport=second_stub) == 0
        assert len(second_stub.calls) == 4
        assert len(list((tmp_path / "cache").glob("*.json"))) == 4


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
    stub = _chunk_aware_transport(
        transport_stub([(200, {}, _response_body("pageviews.200.json"))] * 4)
    )

    assert main(_args(spec_example_path, tmp_path / "out"), transport=stub) == 0

    warning = "pl-post-przerywany: corrupt cache entry; refetching from network"
    assert warning in capsys.readouterr().err
    assert len(stub.calls) == 4
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timestamp", "2024092301"),
        ("timestamp", "2024092200"),
        ("timestamp", "2025092300"),
        ("views", -1),
    ],
)
def test_response_parser_rejects_invalid_daily_item(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
    field,
    value,
):
    _configure(monkeypatch, tmp_path)
    spec_path = _single_series_spec(
        spec_example_path,
        tmp_path,
        start="20240923",
        end="20250922",
    )
    item = {"timestamp": "2024092300", "views": 21}
    item[field] = value
    body = json.dumps({"items": [item]}).encode("utf-8")
    stub = transport_stub([(200, {}, body)])
    out_dir = tmp_path / "out"

    assert main(_args(spec_path, out_dir), transport=stub) == 1

    assert not (out_dir / "series.csv").exists()
    assert list((tmp_path / "cache").glob("*.json")) == []


def test_cached_item_with_invalid_data_is_deleted_refetched_and_replaced(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
    capsys,
):
    _configure(monkeypatch, tmp_path)
    spec_path = _single_series_spec(
        spec_example_path,
        tmp_path,
        start="20240923",
        end="20250922",
    )
    invalid_response = {
        "items": [{"timestamp": "2024092301", "views": 23}]
    }
    cache_path = _write_cache(
        spec_path,
        invalid_response,
        datetime.now(timezone.utc),
        chunk_aware=False,
    )
    valid_body = json.dumps(
        {"items": [{"timestamp": "2024092300", "views": 24}]}
    ).encode("utf-8")
    stub = transport_stub([(200, {}, valid_body)])
    out_dir = tmp_path / "out"

    assert main(_args(spec_path, out_dir), transport=stub) == 0

    assert len(stub.calls) == 1
    assert "invalid cache entry" in capsys.readouterr().err
    replacement = json.loads(cache_path.read_text(encoding="utf-8"))
    assert replacement["status"] == 200
    assert replacement["response"]["items"][0]["timestamp"] == "2024092300"
    with (out_dir / "series.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [(row["date"], row["views"]) for row in rows] == [("2024-09-23", "24")]

    def network_forbidden(url, headers, timeout=30.0):
        raise AssertionError("valid replacement cache should not reach transport")

    assert main(_args(spec_path, out_dir), transport=network_forbidden) == 0


def test_invalid_cache_refetch_does_not_replace_after_invalid_200(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
):
    _configure(monkeypatch, tmp_path)
    spec_path = _single_series_spec(
        spec_example_path,
        tmp_path,
        start="20240923",
        end="20250922",
    )
    cache_path = _write_cache(
        spec_path,
        {"items": [{"timestamp": "2024092200", "views": 25}]},
        datetime.now(timezone.utc),
        chunk_aware=False,
    )
    invalid_body = json.dumps(
        {"items": [{"timestamp": "2024092301", "views": 26}]}
    ).encode("utf-8")
    stub = transport_stub([(200, {}, invalid_body)])
    out_dir = tmp_path / "out"

    assert main(_args(spec_path, out_dir), transport=stub) == 1

    assert len(stub.calls) == 1
    assert not cache_path.exists()
    assert not (out_dir / "series.csv").exists()


def _single_series_spec(
    source_spec: Path,
    tmp_path: Path,
    *,
    start: str,
    end: str,
    fail_on_empty_series: bool = False,
    name: str = "single-series.json",
) -> Path:
    spec = copy.deepcopy(json.loads(source_spec.read_text(encoding="utf-8")))
    spec["series"] = spec["series"][:1]
    spec["window"]["start"] = start
    spec["window"]["end"] = end
    spec["quality"] = {"fail_on_empty_series": fail_on_empty_series}
    spec_path = tmp_path / name
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    return spec_path


def test_multi_chunk_404_preserves_successful_chunks(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
    capsys,
):
    _configure(monkeypatch, tmp_path)
    spec_path = _single_series_spec(
        spec_example_path,
        tmp_path,
        start="20240923",
        end="20250923",
        fail_on_empty_series=True,
    )
    first_chunk_body = json.dumps(
        {"items": [{"timestamp": "2024092300", "views": 11}]}
    ).encode("utf-8")
    stub = transport_stub(
        [
            (200, {}, first_chunk_body),
            (404, {}, _response_body("pageviews.404.json")),
        ]
    )
    out_dir = tmp_path / "out"

    assert main(
        _args(spec_path, out_dir),
        transport=stub,
        today_utc=date(2025, 9, 24),
    ) == 0

    with (out_dir / "series.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [(row["date"], row["views"]) for row in rows] == [("2024-09-23", "11")]
    assert len(stub.calls) == 2
    captured = capsys.readouterr()
    assert "data not yet loaded — retry later" in captured.err
    assert "failures: 0" in captured.out


def test_multi_chunk_all_404_fail_on_empty_series_is_fatal_after_full_scan(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
    capsys,
):
    _configure(monkeypatch, tmp_path)
    spec_path = _single_series_spec(
        spec_example_path,
        tmp_path,
        start="20240923",
        end="20250923",
        fail_on_empty_series=True,
    )
    stub = transport_stub(
        [(404, {}, _response_body("pageviews.404.json"))] * 2
    )
    out_dir = tmp_path / "out"

    assert main(
        _args(spec_path, out_dir),
        transport=stub,
        today_utc=date(2025, 9, 24),
    ) == 1

    assert len(stub.calls) == 2
    assert not (out_dir / "series.csv").exists()
    assert capsys.readouterr().err.count("data not yet loaded — retry later") == 2


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
    stub = _chunk_aware_transport(
        transport_stub(
            [
                (404, {}, _response_body("pageviews.404.json")),
                (200, {}, _response_body("pageviews.200.json")),
            ]
        )
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
    stub = _chunk_aware_transport(
        transport_stub(
            [
                (429, {"Retry-After": "3"}, b"rate limited"),
                (200, {}, _response_body("pageviews.200.json")),
                (429, {"Retry-After": "3"}, b"rate limited"),
                (200, {}, _response_body("pageviews.200.json")),
                (429, {"Retry-After": "3"}, b"rate limited"),
                (200, {}, _response_body("pageviews.200.json")),
                (429, {"Retry-After": "3"}, b"rate limited"),
                (200, {}, _response_body("pageviews.200.json")),
            ]
        )
    )

    assert main(_args(spec_example_path, tmp_path / "out"), transport=stub) == 0
    assert sleeps == [3.0, 3.0, 3.0, 3.0]
    assert len(stub.calls) == 8


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
    stub = _chunk_aware_transport(
        transport_stub(
            [
                (429, {"Retry-After": format_datetime(retry_at, usegmt=True)}, b"rate limited"),
                (200, {}, _response_body("pageviews.200.json")),
                (429, {"Retry-After": format_datetime(retry_at, usegmt=True)}, b"rate limited"),
                (200, {}, _response_body("pageviews.200.json")),
                (429, {"Retry-After": format_datetime(retry_at, usegmt=True)}, b"rate limited"),
                (200, {}, _response_body("pageviews.200.json")),
                (429, {"Retry-After": format_datetime(retry_at, usegmt=True)}, b"rate limited"),
                (200, {}, _response_body("pageviews.200.json")),
            ]
        )
    )

    assert main(_args(spec_example_path, tmp_path / "out"), transport=stub) == 0
    assert len(sleeps) == 4
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
    stub = _chunk_aware_transport(
        transport_stub(
            [
                (429, {}, b"rate limited"),
                (200, {}, _response_body("pageviews.200.json")),
                (429, {}, b"rate limited"),
                (200, {}, _response_body("pageviews.200.json")),
                (429, {}, b"rate limited"),
                (200, {}, _response_body("pageviews.200.json")),
                (429, {}, b"rate limited"),
                (200, {}, _response_body("pageviews.200.json")),
            ]
        )
    )

    assert main(_args(spec_example_path, tmp_path / "out"), transport=stub) == 0
    assert sleeps == [5.0, 5.0, 5.0, 5.0]


def test_retry_cap_marks_series_failed_and_continues(
    spec_example_path,
    tmp_path,
    monkeypatch,
    transport_stub,
):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(fetch_pageviews, "sleep", lambda _seconds: None)
    stub = _chunk_aware_transport(
        transport_stub(
            [
                (429, {}, b"rate limited"),
                (429, {}, b"rate limited"),
                (429, {}, b"rate limited"),
                (200, {}, _response_body("pageviews.200.json")),
                (200, {}, _response_body("pageviews.200.json")),
                (200, {}, _response_body("pageviews.200.json")),
            ]
        )
    )

    assert main(_args(spec_example_path, tmp_path / "out"), transport=stub) == 3
    assert len(stub.calls) == 6
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
    stub = transport_stub([(429, {}, b"rate limited")] * 12)

    assert main(_args(spec_example_path, tmp_path / "out"), transport=stub) == 1
    assert len(stub.calls) == 12
    assert not (tmp_path / "out" / "series.csv").exists()
    assert list((tmp_path / "cache").glob("*.json")) == []


def test_transport_exception_retries_then_preserves_successful_sibling(
    spec_example_path,
    tmp_path,
    monkeypatch,
    capsys,
):
    _configure(monkeypatch, tmp_path)
    sleeps: list[float] = []
    monkeypatch.setattr(fetch_pageviews, "sleep", sleeps.append)
    calls: list[tuple[str, dict[str, str]]] = []
    valid_body = json.dumps(
        {"items": [{"timestamp": "2024092300", "views": 17}]}
    ).encode("utf-8")

    def raising_transport(url, headers, timeout=30.0):
        calls.append((url, dict(headers)))
        if "/Post_przerywany/" in url:
            raise fetch_pageviews.FetchTransportError("offline")
        return fetch_pageviews.TransportResponse(status=200, headers={}, body=valid_body)

    spec = copy.deepcopy(json.loads(spec_example_path.read_text(encoding="utf-8")))
    spec["window"]["start"] = "20240923"
    spec["window"]["end"] = "20250922"
    spec_path = tmp_path / "transport-siblings.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    assert main(
        _args(spec_path, tmp_path / "out"),
        transport=raising_transport,
        today_utc=date(2026, 9, 24),
    ) == 3

    assert len(calls) == 4
    assert sleeps == [5.0, 5.0]
    with (tmp_path / "out" / "series.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [(row["series_id"], row["date"], row["views"]) for row in rows] == [
        ("cs-pust-prerusovany", "2024-09-23", "17")
    ]
    captured = capsys.readouterr()
    assert "pl-post-przerywany: request failed after 3 attempt(s) (transport error)" in captured.err
    assert "offline" not in captured.err


def test_transport_exception_recovers_on_next_attempt(
    spec_example_path,
    tmp_path,
    monkeypatch,
):
    _configure(monkeypatch, tmp_path)
    sleeps: list[float] = []
    monkeypatch.setattr(fetch_pageviews, "sleep", sleeps.append)
    calls: list[str] = []
    valid_body = json.dumps(
        {"items": [{"timestamp": "2024092300", "views": 19}]}
    ).encode("utf-8")

    def transient_transport(url, headers, timeout=30.0):
        calls.append(url)
        if len(calls) == 1:
            raise fetch_pageviews.FetchTransportError("temporary offline")
        return fetch_pageviews.TransportResponse(status=200, headers={}, body=valid_body)

    spec_path = _single_series_spec(
        spec_example_path,
        tmp_path,
        start="20240923",
        end="20250922",
    )

    assert main(
        _args(spec_path, tmp_path / "out"),
        transport=transient_transport,
        today_utc=date(2026, 9, 24),
    ) == 0

    assert len(calls) == 2
    assert sleeps == [5.0]
    assert (tmp_path / "out" / "series.csv").exists()


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
    stub = _chunk_aware_transport(
        transport_stub(
            [
                (429, {}, b"rate limited"),
                (429, {}, b"rate limited"),
                (429, {}, b"rate limited"),
                (200, {}, _response_body("pageviews.200.json")),
                (200, {}, _response_body("pageviews.200.json")),
                (200, {}, _response_body("pageviews.200.json")),
            ]
        )
    )

    assert main(_args(spec_example_path, tmp_path / "out"), transport=stub) == 3

    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 1
    assert "series=2" not in captured.out
    diagnostic_lines = captured.err.splitlines()
    assert diagnostic_lines
    assert all(line.startswith(("pl-post-przerywany: ", "cs-pust-prerusovany: ")) for line in diagnostic_lines)
    assert "rate limited" not in " ".join(diagnostic_lines)
