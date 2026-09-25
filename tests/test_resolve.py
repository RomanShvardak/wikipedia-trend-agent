"""Offline resolver discovery and confirmation contract tests.

Every integration path injects a scripted or forbidden transport. Production
code must never fall back to the network in this module.
"""
from __future__ import annotations

import copy
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


def test_completed_zero_hit_search_publishes_empty_provenance(
    tmp_path, monkeypatch, transport_stub
):
    _configure(monkeypatch, tmp_path)
    out = tmp_path / "resolved.json"
    stub = transport_stub([(200, {}, b'{"query":{"search":[]}}')])

    assert resolve_articles.main(_args(out), transport=stub, today_utc=TODAY) == 2

    document = json.loads(out.read_text(encoding="utf-8"))
    assert len(stub.calls) == 1
    assert document["status"] == "unresolved"
    project = document["projects"][0]
    assert project["search_hits"] == []
    assert project["candidates"] == []
    assert project["status"] == "unresolved"
    assert project["reason"] == "search_no_hits"


def test_external_display_text_strips_c0_c1_and_caps_codepoints():
    cleaned = resolve_articles._clean_text("A\x00B\x85" + "x" * 300, "title")

    assert cleaned == "AB" + "x" * 254
    assert len(cleaned) == 256


def _run_discovery(
    out: Path,
    tmp_path: Path,
    monkeypatch,
    transport_stub,
    resolve_fixture,
    *,
    extra_args: list[str] | None = None,
) -> None:
    _configure(monkeypatch, tmp_path)
    stub = transport_stub(
        [
            (200, {}, _response_body(resolve_fixture, "resolve.search.en.wikipedia.json")),
            (200, {}, _response_body(resolve_fixture, "resolve.redirects.en.wikipedia.json")),
            (200, {}, _volume_body(resolve_fixture)),
        ]
    )
    assert resolve_articles.main(
        [*_args(out), *(extra_args or [])],
        transport=stub,
        today_utc=TODAY,
    ) == 0
    assert len(stub.calls) == 3


def _forbid_confirmation_side_effects(monkeypatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("confirmation must remain offline")

    monkeypatch.setattr(common, "user_agent", forbidden)
    monkeypatch.setattr(common, "throttle", forbidden)
    monkeypatch.setattr(common, "read_json_cache", forbidden)
    monkeypatch.setattr(common, "write_json_cache", forbidden)


def test_confirm_exact_saved_candidate_is_offline_and_atomic(
    tmp_path,
    monkeypatch,
    transport_stub,
    resolve_fixture,
    forbidden_transport,
):
    assert hasattr(resolve_articles, "confirm"), "confirm state transition must be public"
    out = tmp_path / "resolved.json"
    _run_discovery(out, tmp_path, monkeypatch, transport_stub, resolve_fixture)
    discovery = json.loads(out.read_text(encoding="utf-8"))
    _forbid_confirmation_side_effects(monkeypatch)
    publications: list[Path] = []
    real_dump_json = common.dump_json
    monkeypatch.setattr(
        common,
        "dump_json",
        lambda document, path: (
            publications.append(Path(path)),
            real_dump_json(document, path),
        )[1],
    )
    args = [*_args(out), "--select", "en.wikipedia=Intermittent_fasting"]

    assert resolve_articles.main(args, transport=forbidden_transport) == 0

    confirmed = json.loads(out.read_text(encoding="utf-8"))
    expected = copy.deepcopy(discovery)
    expected["run_mode"] = "confirm"
    expected["status"] = "confirmed"
    expected["projects"][0]["selection"] = {
        "article": "Intermittent_fasting",
        "title": "Intermittent fasting",
        "reason": None,
        "source": "model_confirmation",
    }
    assert confirmed == expected
    assert publications == [out]
    assert sorted(path.name for path in out.parent.iterdir()) == ["resolved.json"]


def test_confirm_rejects_slug_outside_saved_candidates_without_replacing_bytes(
    tmp_path,
    monkeypatch,
    transport_stub,
    resolve_fixture,
    forbidden_transport,
):
    out = tmp_path / "resolved.json"
    _run_discovery(out, tmp_path, monkeypatch, transport_stub, resolve_fixture)
    before = out.read_bytes()
    _forbid_confirmation_side_effects(monkeypatch)
    args = [*_args(out), "--select", "en.wikipedia=Invented_Article"]

    assert resolve_articles.main(args, transport=forbidden_transport) == 2
    assert out.read_bytes() == before


def test_confirm_rejects_wrong_topic_without_replacing_bytes(
    tmp_path,
    monkeypatch,
    transport_stub,
    resolve_fixture,
    forbidden_transport,
):
    out = tmp_path / "resolved.json"
    _run_discovery(out, tmp_path, monkeypatch, transport_stub, resolve_fixture)
    before = out.read_bytes()
    _forbid_confirmation_side_effects(monkeypatch)
    args = [
        "--topic",
        "different topic",
        "--projects",
        "en.wikipedia",
        "--out",
        str(out),
        "--select",
        "en.wikipedia=Intermittent_fasting",
    ]

    assert resolve_articles.main(args, transport=forbidden_transport) == 2
    assert out.read_bytes() == before


def test_confirm_rejects_saved_project_context_mismatch_without_replacing_bytes(
    tmp_path,
    monkeypatch,
    transport_stub,
    resolve_fixture,
    forbidden_transport,
):
    out = tmp_path / "resolved.json"
    _run_discovery(out, tmp_path, monkeypatch, transport_stub, resolve_fixture)
    document = json.loads(out.read_text(encoding="utf-8"))
    document["projects"][0]["project"] = "pl.wikipedia"
    common.dump_json(document, out)
    before = out.read_bytes()
    _forbid_confirmation_side_effects(monkeypatch)
    args = [*_args(out), "--select", "en.wikipedia=Intermittent_fasting"]

    assert resolve_articles.main(args, transport=forbidden_transport) == 2
    assert out.read_bytes() == before


def test_confirm_requires_replaying_project_override_context(
    tmp_path,
    monkeypatch,
    transport_stub,
    resolve_fixture,
    forbidden_transport,
):
    out = tmp_path / "resolved.json"
    _run_discovery(
        out,
        tmp_path,
        monkeypatch,
        transport_stub,
        resolve_fixture,
        extra_args=["--topic-for", "en.wikipedia=fasting"],
    )
    before = out.read_bytes()
    _forbid_confirmation_side_effects(monkeypatch)
    args = [*_args(out), "--select", "en.wikipedia=Intermittent_fasting"]

    assert resolve_articles.main(args, transport=forbidden_transport) == 2
    assert out.read_bytes() == before


def test_confirm_rejects_duplicate_selection_during_preflight(
    tmp_path,
    monkeypatch,
    transport_stub,
    resolve_fixture,
    forbidden_transport,
):
    out = tmp_path / "resolved.json"
    _run_discovery(out, tmp_path, monkeypatch, transport_stub, resolve_fixture)
    before = out.read_bytes()
    _forbid_confirmation_side_effects(monkeypatch)
    args = [
        *_args(out),
        "--select",
        "en.wikipedia=Intermittent_fasting",
        "--select",
        "en.wikipedia=Intermittent_fasting",
    ]

    assert resolve_articles.main(args, transport=forbidden_transport) == 2
    assert out.read_bytes() == before


def test_confirm_direct_api_rejects_missing_selection_without_replacing_bytes(
    tmp_path,
    monkeypatch,
    transport_stub,
    resolve_fixture
):
    out = tmp_path / "resolved.json"
    _run_discovery(out, tmp_path, monkeypatch, transport_stub, resolve_fixture)
    before = out.read_bytes()
    _forbid_confirmation_side_effects(monkeypatch)

    document, exit_code = resolve_articles.confirm(
        topic="intermittent fasting",
        projects=["en.wikipedia"],
        topic_overrides=[],
        selections={},
        reason=None,
        out=out,
    )

    assert document is None
    assert exit_code == 2
    assert out.read_bytes() == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("contract_version", "resolved.invalid"),
        ("run_mode", "confirm"),
        ("status", "confirmed"),
    ],
)
def test_confirm_rejects_stale_manifest_state_without_replacing_bytes(
    field,
    value,
    tmp_path,
    monkeypatch,
    transport_stub,
    resolve_fixture,
    forbidden_transport,
):
    out = tmp_path / "resolved.json"
    _run_discovery(out, tmp_path, monkeypatch, transport_stub, resolve_fixture)
    document = json.loads(out.read_text(encoding="utf-8"))
    document[field] = value
    common.dump_json(document, out)
    before = out.read_bytes()
    _forbid_confirmation_side_effects(monkeypatch)
    args = [*_args(out), "--select", "en.wikipedia=Intermittent_fasting"]

    assert resolve_articles.main(args, transport=forbidden_transport) == 2
    assert out.read_bytes() == before


def test_confirm_requires_reason_for_ambiguous_saved_project(
    tmp_path,
    monkeypatch,
    transport_stub,
    resolve_fixture,
    forbidden_transport,
):
    out = tmp_path / "resolved.json"
    _run_discovery(out, tmp_path, monkeypatch, transport_stub, resolve_fixture)
    document = json.loads(out.read_text(encoding="utf-8"))
    document["projects"][0]["status"] = "ambiguous"
    document["projects"][0]["recommendation"] = None
    common.dump_json(document, out)
    before = out.read_bytes()
    _forbid_confirmation_side_effects(monkeypatch)
    args = [*_args(out), "--select", "en.wikipedia=Intermittent_fasting"]

    assert resolve_articles.main(args, transport=forbidden_transport) == 2
    assert out.read_bytes() == before

    reason_args = [*args, "--reason", "Explicit semantic match"]
    assert resolve_articles.main(reason_args, transport=forbidden_transport) == 0
    confirmed = json.loads(out.read_text(encoding="utf-8"))
    assert confirmed["projects"][0]["selection"]["reason"] == "Explicit semantic match"


def _metadata_payload(
    *,
    normalized: list[dict[str, str]] | None = None,
    redirects: list[dict[str, str]] | None = None,
    pages: list[dict[str, object]] | None = None,
) -> bytes:
    return json.dumps(
        {
            "query": {
                "normalized": normalized or [],
                "redirects": redirects or [],
                "pages": pages or [],
            }
        },
        ensure_ascii=False,
    ).encode("utf-8")


def test_redirect_chain_preserves_normalized_order_and_final_target():
    assert hasattr(resolve_articles, "follow_redirects")
    assert hasattr(resolve_articles, "parse_metadata")
    body = _metadata_payload(
        normalized=[{"from": "old name", "to": "Old Name"}],
        redirects=[
            {"from": "Old Name", "to": "Middle title"},
            {"from": "Middle title", "to": "Canonical Target"},
        ],
        pages=[{"ns": 0, "title": "Canonical Target", "pageprops": {}}],
    )

    final_title, chain, reason = resolve_articles.follow_redirects(
        "old name",
        {"old name": "Old Name"},
        {"Old Name": "Middle title", "Middle title": "Canonical Target"},
    )
    candidates = resolve_articles.parse_metadata(
        body,
        [{"title": "old name", "rank": 1}],
        "old name",
    )

    assert (final_title, chain, reason) == (
        "Canonical Target",
        ["Middle title", "Canonical Target"],
        None,
    )
    assert candidates == [
        {
            "status": "selectable",
            "article": "Canonical_Target",
            "title": "Canonical Target",
            "namespace": 0,
            "input_titles": ["old name"],
            "redirect_chain": ["Middle title", "Canonical Target"],
            "search_rank": 1,
            "exact_title_match": False,
            "disambiguation": False,
            "reason": None,
        }
    ]


def test_redirect_cycle_is_unresolved_with_stable_reason():
    assert hasattr(resolve_articles, "follow_redirects")

    final_title, chain, reason = resolve_articles.follow_redirects(
        "A",
        {},
        {"A": "B", "B": "A"},
    )

    assert (final_title, chain, reason) == ("A", ["B", "A"], "redirect_cycle")


def test_redirect_eleventh_hop_is_unresolved_with_stable_reason():
    assert hasattr(resolve_articles, "follow_redirects")
    redirects = {f"Title {index}": f"Title {index + 1}" for index in range(11)}
    redirects["Title 11"] = "Canonical"

    final_title, chain, reason = resolve_articles.follow_redirects(
        "Title 0", {}, redirects
    )

    assert final_title == "Title 11"
    assert len(chain) == 11
    assert reason == "redirect_too_deep"


@pytest.mark.parametrize(
    ("page", "expected_reason"),
    [
        ({"missing": True, "ns": 0, "title": "Missing"}, "missing_target"),
        ({"ns": 14, "title": "Category:Target"}, "non_article_namespace"),
        (
            {"ns": 0, "title": "Target", "pageprops": {"disambiguation": ""}},
            "disambiguation_page",
        ),
    ],
)
def test_invalid_metadata_target_is_retained_as_unresolved(page, expected_reason):
    assert hasattr(resolve_articles, "parse_metadata")
    body = _metadata_payload(
        redirects=(
            []
            if page.get("missing") is True
            else [{"from": "Requested", "to": str(page["title"])}]
        ),
        pages=[page],
    )

    candidates = resolve_articles.parse_metadata(
        body,
        [{"title": "Requested", "rank": 1}],
        "Requested",
    )

    assert len(candidates) == 1
    assert candidates[0]["status"] == "unresolved"
    assert candidates[0]["reason"] == expected_reason


def test_redirect_discovery_calls_aqs_only_for_final_canonical_target(
    tmp_path, monkeypatch, transport_stub, resolve_fixture
):
    _configure(monkeypatch, tmp_path)
    metadata = _metadata_payload(
        redirects=[{"from": "Redirect title", "to": "Canonical fasting"}],
        pages=[{"ns": 0, "title": "Canonical fasting", "pageprops": {}}],
    )
    volume = _volume_body(resolve_fixture).replace(
        b"Intermittent_fasting", b"Canonical_fasting"
    )
    stub = transport_stub(
        [
            (200, {}, b'{"query":{"search":[{"ns":0,"title":"Redirect title"}]}}'),
            (200, {}, metadata),
            (200, {}, volume),
        ]
    )
    out = tmp_path / "resolved.json"

    assert resolve_articles.main(
        ["--topic", "redirect title", "--projects", "en.wikipedia", "--out", str(out)],
        transport=stub,
        today_utc=TODAY,
    ) == 0

    assert stub.calls[-1][0] == series_url(
        "en.wikipedia", "Canonical_fasting", "20260826", "20260924"
    )
    assert "Redirect_title" not in stub.calls[-1][0]
    document = json.loads(out.read_text(encoding="utf-8"))
    candidate = document["projects"][0]["candidates"][0]
    assert candidate["input_titles"] == ["Redirect title"]
    assert candidate["redirect_chain"] == ["Canonical fasting"]
    assert candidate["article"] == "Canonical_fasting"


def test_structurally_invalid_candidate_never_reaches_volume_transport(
    tmp_path, monkeypatch, transport_stub
):
    _configure(monkeypatch, tmp_path)
    metadata = _metadata_payload(
        redirects=[{"from": "Requested", "to": "Category:Target"}],
        pages=[{"ns": 14, "title": "Category:Target"}],
    )
    stub = transport_stub(
        [
            (200, {}, b'{"query":{"search":[{"ns":0,"title":"Requested"}]}}'),
            (200, {}, metadata),
        ]
    )
    out = tmp_path / "resolved.json"

    assert resolve_articles.main(
        ["--topic", "requested", "--projects", "en.wikipedia", "--out", str(out)],
        transport=stub,
        today_utc=TODAY,
    ) == 2

    assert len(stub.calls) == 2
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["status"] == "unresolved"
    assert document["projects"][0]["candidates"][0]["reason"] == "non_article_namespace"
