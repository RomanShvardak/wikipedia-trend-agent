"""Offline resolver discovery and confirmation contract tests.

Every integration path injects a scripted or forbidden transport. Production
code must never fall back to the network in this module.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

import common
import conftest
import resolve_articles
from fetch_pageviews import series_url

DESCRIPTIVE_UA = "wikipedia-trend-agent/0.1.0 (maintainer@invalid.example.net) python-urllib"
TODAY = date(2026, 9, 25)
VOLUME_START = date(2026, 8, 26)
VOLUME_END = date(2026, 9, 24)


def _response_body(resolve_fixture, name: str) -> bytes:
    return resolve_fixture(name)


def _volume_body(resolve_fixture, *, views_per_day: int = 20) -> bytes:
    template = json.loads(_response_body(resolve_fixture, "pageviews.200.json").decode("utf-8"))
    source = template["items"][0]
    items: list[dict[str, object]] = []
    current = VOLUME_START
    while current <= VOLUME_END:
        items.append(
            {
                **source,
                "project": "en.wikipedia",
                "article": "Intermittent_fasting",
                "timestamp": f"{current:%Y%m%d}00",
                "views": views_per_day,
            }
        )
        current += timedelta(days=1)
    return json.dumps({"items": items}, ensure_ascii=False).encode("utf-8")


def _configure(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WTI_USER_AGENT", DESCRIPTIVE_UA)
    monkeypatch.setattr(common, "CACHE_DIR", tmp_path / "cache")
    throttle_calls: list[list[float]] = []

    def record_throttle(seconds: float, last: list[float]) -> None:
        assert seconds == 1.0
        throttle_calls.append(last)

    monkeypatch.setattr(common, "throttle", record_throttle)


def _args(out: Path) -> list[str]:
    return [
        "--topic",
        "intermittent fasting",
        "--projects",
        "en.wikipedia",
        "--out",
        str(out),
    ]


def test_resolve_bootstrap_guards_are_exposed():
    assert hasattr(conftest, "forbidden_transport")
    assert hasattr(conftest, "resolve_fixture")
    assert hasattr(conftest, "_forbidden_transport")
    assert hasattr(conftest, "_resolve_fixture")


def test_forbidden_transport_fails_on_first_call(forbidden_transport):
    with pytest.raises(AssertionError, match="network access is forbidden"):
        forbidden_transport(
            "https://en.wikipedia.org/w/api.php",
            {"User-Agent": "test"},
            30.0,
        )


def test_resolve_fixture_loads_committed_utf8_json(resolve_fixture):
    body = resolve_fixture("pageviews.200.json")
    payload = json.loads(body.decode("utf-8"))

    assert payload["items"][0]["views"] == 1868


@pytest.mark.parametrize(
    "name",
    [
        "../conftest.py",
        "../../AGENTS.md",
        str((Path(__file__).resolve().parents[2] / "AGENTS.md")),
    ],
)
def test_resolve_fixture_rejects_escaping_paths(resolve_fixture, name):
    with pytest.raises(AssertionError):
        resolve_fixture(name)


def test_single_project_tracer(
    tmp_path,
    monkeypatch,
    transport_stub,
    resolve_fixture,
):
    _configure(monkeypatch, tmp_path)
    out = tmp_path / "out" / "resolved.json"
    stub = transport_stub(
        [
            (200, {}, _response_body(resolve_fixture, "resolve.search.en.wikipedia.json")),
            (200, {}, _response_body(resolve_fixture, "resolve.redirects.en.wikipedia.json")),
            (200, {}, _volume_body(resolve_fixture)),
        ]
    )
    publications: list[tuple[object, Path]] = []
    real_dump_json = common.dump_json
    monkeypatch.setattr(
        common,
        "dump_json",
        lambda document, path: (
            publications.append((document, Path(path))),
            real_dump_json(document, path),
        )[1],
    )

    assert resolve_articles.main(_args(out), transport=stub, today_utc=TODAY) == 0

    assert len(stub.calls) == 3
    search = urlparse(stub.calls[0][0])
    search_params = parse_qs(search.query)
    assert (search.scheme, search.netloc, search.path) == (
        "https",
        "en.wikipedia",
        "/w/api.php",
    )
    assert search_params == {
        "action": ["query"],
        "format": ["json"],
        "formatversion": ["2"],
        "errorformat": ["plaintext"],
        "list": ["search"],
        "srsearch": ["intermittent fasting"],
        "srnamespace": ["0"],
        "srlimit": ["5"],
        "srsort": ["relevance"],
    }
    metadata_params = parse_qs(urlparse(stub.calls[1][0]).query)
    assert metadata_params["redirects"] == ["1"]
    assert metadata_params["titles"] == ["intermittent fasting|Intermittent fasting"]
    assert metadata_params["prop"] == ["info|pageprops"]
    assert metadata_params["inprop"] == ["url"]
    assert metadata_params["ppprop"] == ["disambiguation"]
    assert stub.calls[2][0] == series_url(
        "en.wikipedia", "Intermittent_fasting", "20260826", "20260924"
    )
    assert all(headers["User-Agent"] == DESCRIPTIVE_UA for _, headers in stub.calls)

    raw = out.read_bytes()
    document = json.loads(raw.decode("utf-8"))
    assert len(publications) == 1
    assert publications[0][1] == out
    assert set(document) == {
        "contract_version",
        "run_mode",
        "status",
        "topic",
        "generated_at",
        "volume_window",
        "projects",
    }
    assert document["contract_version"] == "resolved.v1"
    assert document["run_mode"] == "discover"
    assert document["status"] == "awaiting_confirmation"
    assert document["topic"] == "intermittent fasting"
    assert document["volume_window"] == {
        "start": "2026-08-26",
        "end": "2026-09-24",
        "days": 30,
        "access": "all-access",
        "agent": "user",
        "granularity": "daily",
    }
    assert len(document["projects"]) == 1
    project = document["projects"][0]
    assert project == {
        "project": "en.wikipedia",
        "effective_query": "intermittent fasting",
        "query_source": "shared",
        "status": "ready",
        "recommendation": "Intermittent_fasting",
        "search_hits": [{"title": "Intermittent fasting", "rank": 1}],
        "candidates": [
            {
                "status": "selectable",
                "article": "Intermittent_fasting",
                "title": "Intermittent fasting",
                "namespace": 0,
                "input_titles": ["Intermittent fasting"],
                "redirect_chain": [],
                "search_rank": 1,
                "exact_title_match": True,
                "disambiguation": False,
                "volume": {
                    "status": "available",
                    "total_views": 600,
                    "window": {"start": "2026-08-26", "end": "2026-09-24"},
                    "observed_days": 30,
                    "last_observed_date": "2026-09-24",
                    "low_volume": True,
                    "reason": None,
                },
                "reason": None,
            }
        ],
        "selection": None,
        "reason": None,
        "error": None,
    }
    assert sorted(path.name for path in out.parent.iterdir()) == ["resolved.json"]


def test_search_url_uses_exact_unicode_without_normalization():
    query = "Cafe\u0301 東京"
    url = resolve_articles.search_url("en.wikipedia", query)
    parsed = urlparse(url)

    assert (parsed.scheme, parsed.netloc, parsed.path) == (
        "https",
        "en.wikipedia",
        "/w/api.php",
    )
    assert parse_qs(parsed.query)["srsearch"] == [query]
    assert "srwhat" not in parse_qs(parsed.query)
    assert resolve_articles.canonical_article("Cafe\u0301 東京") == "Cafe%CC%81_%E6%9D%B1%E4%BA%AC"


def test_preflight_rejects_empty_topic_before_user_agent_and_transport(
    tmp_path, monkeypatch, transport_stub
):
    def forbidden_user_agent() -> str:
        raise AssertionError("user_agent must not run before input validation")

    monkeypatch.setattr(common, "user_agent", forbidden_user_agent)
    stub = transport_stub([])
    out = tmp_path / "resolved.json"

    args = ["--topic", "", "--projects", "en.wikipedia", "--out", str(out)]
    assert resolve_articles.main(args, transport=stub) == 2
    assert stub.calls == []
    assert not out.exists()


def test_preflight_rejects_empty_project_set_without_serialization():
    with pytest.raises(resolve_articles.ResolveInputError, match="at least one project"):
        resolve_articles.preflight_inputs("topic", [], [], [])


def test_preflight_rejects_none_project_value_without_serialization():
    with pytest.raises(resolve_articles.ResolveInputError, match="must be a string"):
        resolve_articles.preflight_inputs("topic", [None], [], [])  # type: ignore[list-item]


def test_preflight_rejects_null_catalog_entry_before_transport(
    tmp_path, monkeypatch, transport_stub
):
    catalog = tmp_path / "projects.json"
    catalog.write_text(
        json.dumps({"schema_version": "wikipedia-projects.v1", "projects": [None]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(resolve_articles, "PROJECTS_FILE", catalog)
    stub = transport_stub([])
    out = tmp_path / "resolved.json"

    assert resolve_articles.main(_args(out), transport=stub) == 2
    assert stub.calls == []
    assert not out.exists()


@pytest.mark.parametrize(
    "project",
    ["https://en.wikipedia.org/w/api.php", "not-a-project.wikipedia", "evil.example"],
)
def test_preflight_rejects_host_shaped_and_unallowlisted_projects(
    project, tmp_path, transport_stub
):
    stub = transport_stub([])
    out = tmp_path / "resolved.json"
    args = ["--topic", "topic", "--projects", project, "--out", str(out)]

    assert resolve_articles.main(args, transport=stub) == 2
    assert stub.calls == []
    assert not out.exists()


@pytest.mark.parametrize(
    "body",
    [
        b'{"error":{"code":"badrequest","info":"bad request"}}',
        b'{"errors":[{"code":"search-title-disabled","module":"cirrussearch"}]}',
    ],
)
def test_api_error_http_200_creates_no_manifest_or_cache(
    body, tmp_path, monkeypatch, transport_stub
):
    _configure(monkeypatch, tmp_path)
    stub = transport_stub([(200, {}, body)])
    out = tmp_path / "resolved.json"

    assert resolve_articles.main(_args(out), transport=stub, today_utc=TODAY) == 1
    assert not out.exists()
    assert list((tmp_path / "cache").glob("*.json")) == []


def test_bounded_transport_reads_only_limit_plus_one(monkeypatch):
    limit = resolve_articles.MAX_RESPONSE_BYTES

    class OverLimitStream:
        status = 200
        headers: dict[str, str] = {}

        def __init__(self) -> None:
            self.read_sizes: list[int | None] = []

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, size: int | None = None) -> bytes:
            self.read_sizes.append(size)
            return b"x" * (limit + 1)

    stream = OverLimitStream()
    monkeypatch.setattr(common.urllib.request, "urlopen", lambda *_args, **_kwargs: stream)

    with pytest.raises(common.ResponseTooLarge):
        resolve_articles.bounded_transport("https://en.wikipedia.org/w/api.php", {})

    assert stream.read_sizes == [limit + 1]
