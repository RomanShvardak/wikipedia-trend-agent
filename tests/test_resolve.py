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
            (200, {}, _response_body(resolve_fixture, "resolve.normalized.en.wikipedia.json")),
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
        "en.wikipedia.org",
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
    # Derived from the committed search evidence so a refreshed capture cannot
    # silently desync the batched metadata request: the raw topic first, then one
    # entry per ordered ns=0 hit. The real `srlimit=5` response is captured
    # separately as resolve.search.batched.en.wikipedia.json evidence.
    search_fixture = json.loads(
        _response_body(resolve_fixture, "resolve.search.en.wikipedia.json").decode("utf-8")
    )
    hit_titles = [hit["title"] for hit in search_fixture["query"]["search"]]
    assert hit_titles == ["Intermittent fasting"]
    assert all(hit["ns"] == 0 for hit in search_fixture["query"]["search"])
    assert metadata_params["titles"] == [
        "|".join(["intermittent fasting", *hit_titles])
    ]
    assert metadata_params["prop"] == ["info|pageprops"]
    assert metadata_params["inprop"] == ["url"]
    assert metadata_params["ppprop"] == ["disambiguation"]
    assert stub.calls[2][0] == series_url(
        "en.wikipedia", "Intermittent_fasting", "20260826", "20260924"
    )
    assert all(headers["User-Agent"] == DESCRIPTIVE_UA for _, headers in stub.calls)

    raw = out.read_bytes()
    document = json.loads(raw.decode("utf-8"))
    manifest_publications = [path for _document, path in publications if path == out]
    assert manifest_publications == [out]
    assert set(document) == {
        "contract_version",
        "run_mode",
        "status",
        "topic",
        "topic_overrides",
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
    assert sorted(
        path.name for path in out.parent.iterdir() if path.is_file()
    ) == ["resolved.json"]


def test_search_url_uses_exact_unicode_without_normalization():
    query = "Cafe\u0301 東京"
    url = resolve_articles.search_url("en.wikipedia", query)
    parsed = urlparse(url)

    assert (parsed.scheme, parsed.netloc, parsed.path) == (
        "https",
        "en.wikipedia.org",
        "/w/api.php",
    )
    assert parse_qs(parsed.query)["srsearch"] == [query]
    assert "srwhat" not in parse_qs(parsed.query)
    assert resolve_articles.canonical_article("Cafe\u0301 東京") == "Cafe%CC%81_%E6%9D%B1%E4%BA%AC"


def test_action_api_host_is_site_matrix_shape_not_a_bare_catalog_stem():
    """The catalog stores Site Matrix host stems; the host is the stem plus .org.

    04-08: `action_api_url` used the stem verbatim as the host and produced
    `https://en.wikipedia/...`, which does not resolve, while the official Site
    Matrix publishes `https://en.wikipedia.org` for that edition. This test binds
    every Action URL builder to the catalog, so the stem-as-host reconstruction
    cannot be reintroduced or re-pinned as expected behavior.
    """
    catalog = json.loads(
        (Path(__file__).resolve().parents[1] / "assets" / "wikipedia-projects.json").read_text(
            encoding="utf-8"
        )
    )
    projects = catalog["projects"]
    assert len(projects) == 364
    sampled = [projects[0], "en.wikipedia", "pl.wikipedia", "zh-yue.wikipedia", projects[-1]]

    for project in sampled:
        assert project in resolve_articles.load_allowed_projects()
        expected_netloc = f"{project}.org"
        for url in (
            resolve_articles.action_api_url(project, {"action": "query"}),
            resolve_articles.search_url(project, "topic"),
            resolve_articles.metadata_url(project, ["Topic"]),
        ):
            parsed = urlparse(url)
            assert parsed.scheme == "https", project
            assert parsed.netloc == expected_netloc, project
            assert parsed.path == "/w/api.php", project
            # The stem is an identifier, never a bare host.
            assert parsed.netloc != project, project
            assert parsed.username is None and parsed.password is None, project
            assert parsed.port is None, project


def test_action_api_host_rejects_non_catalog_and_host_shaped_projects():
    for rejected in (
        "en.wikipedia.evil.example",
        "en.wikipedia.org",
        "https://en.wikipedia.org",
        "en.wikipedia:8443",
        "en.wikipedia/w/api.php",
        "user@en.wikipedia",
    ):
        with pytest.raises(resolve_articles.ResolveInputError):
            resolve_articles.action_api_url(rejected, {"action": "query"})


def test_aqs_project_code_stays_a_path_segment_and_is_not_given_a_suffix():
    """AQS carries the bare stem in the path; only the Action host gets `.org`."""
    aqs = urlparse(series_url("en.wikipedia", "Barack_Obama", "20260826", "20260924"))

    assert aqs.netloc == "wikimedia.org"
    assert "/per-article/en.wikipedia/" in aqs.path
    assert ".wikipedia.org" not in aqs.path


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
def test_api_error_http_200_creates_sanitized_error_manifest_or_cache(
    body, tmp_path, monkeypatch, transport_stub
):
    _configure(monkeypatch, tmp_path)
    stub = transport_stub([(200, {}, body)])
    out = tmp_path / "resolved.json"

    assert resolve_articles.main(_args(out), transport=stub, today_utc=TODAY) == 1
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["status"] == "error"
    assert document["projects"][0]["error"]["code"] == "action_api_error"
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
            (200, {}, _response_body(resolve_fixture, "resolve.normalized.en.wikipedia.json")),
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
    assert sorted(
        path.name for path in out.parent.iterdir() if path.is_file()
    ) == ["resolved.json"]


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
    changed_args = [*_args(out), "--topic-for", "en.wikipedia=changed", "--select", "en.wikipedia=Intermittent_fasting"]
    assert resolve_articles.main(changed_args, transport=forbidden_transport) == 2
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
    second_candidate = copy.deepcopy(document["projects"][0]["candidates"][0])
    second_candidate["article"] = "Other_fasting"
    second_candidate["title"] = "Other fasting"
    second_candidate["exact_title_match"] = False
    document["projects"][0]["candidates"].append(second_candidate)
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
def test_missing_target_namespace_and_disambiguation_are_unresolved(page, expected_reason):
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


def test_redirect_cycle_candidate_is_unresolved():
    body = _metadata_payload(
        redirects=[{"from": "A", "to": "B"}, {"from": "B", "to": "A"}],
        pages=[{"ns": 0, "title": "A", "pageprops": {}}],
    )

    candidates = resolve_articles.parse_metadata(
        body, [{"title": "A", "rank": 1}], "A"
    )

    assert candidates[0]["status"] == "unresolved"
    assert candidates[0]["reason"] == "redirect_cycle"
    assert candidates[0]["article"] is None


def test_redirect_too_deep_candidate_is_unresolved():
    redirects = [
        {"from": f"Title {index}", "to": f"Title {index + 1}"}
        for index in range(11)
    ]
    body = _metadata_payload(
        redirects=redirects,
        pages=[{"ns": 0, "title": "Title 11", "pageprops": {}}],
    )

    candidates = resolve_articles.parse_metadata(
        body, [{"title": "Title 0", "rank": 1}], "Title 0"
    )

    assert candidates[0]["status"] == "unresolved"
    assert candidates[0]["reason"] == "redirect_too_deep"
    assert candidates[0]["article"] is None


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


def _volume_item(
    *,
    project: str = "en.wikipedia",
    article: str = "Intermittent_fasting",
    access: str = "all-access",
    agent: str = "user",
    granularity: str = "daily",
    timestamp: str = "2026082600",
    views: object = 10,
) -> dict[str, object]:
    return {
        "project": project,
        "article": article,
        "access": access,
        "agent": agent,
        "granularity": granularity,
        "timestamp": timestamp,
        "views": views,
    }


def _volume_response(
    body: object,
    *,
    status: int = 200,
) -> common.TransportResponse:
    if isinstance(body, bytes):
        encoded = body
    else:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    return common.TransportResponse(status=status, headers={}, body=encoded)


def test_volume_window_is_exactly_30_complete_utc_days():
    assert hasattr(resolve_articles, "volume_window")

    result = resolve_articles.volume_window(TODAY)

    assert result == (VOLUME_START, VOLUME_END)
    assert (result[1] - result[0]).days + 1 == 30
    assert result[1] < TODAY


def test_volume_valid_payload_sums_identity_bound_observations():
    assert hasattr(resolve_articles, "parse_volume_response")
    response = _volume_response(
        {
            "items": [
                _volume_item(timestamp="2026082600", views=10),
                _volume_item(timestamp="2026082700", views=20),
                _volume_item(timestamp="2026083000", views=969),
            ]
        }
    )

    result = resolve_articles.parse_volume_response(
        response,
        expected_project="en.wikipedia",
        expected_article="Intermittent_fasting",
        start=VOLUME_START,
        end=VOLUME_END,
    )

    assert result == {
        "status": "available",
        "total_views": 999,
        "window": {"start": "2026-08-26", "end": "2026-09-24"},
        "observed_days": 3,
        "last_observed_date": "2026-08-30",
        "low_volume": True,
        "reason": None,
    }


def test_volume_404_is_unavailable_not_numeric_zero(resolve_fixture):
    assert hasattr(resolve_articles, "parse_volume_response")
    response = _volume_response(
        _response_body(resolve_fixture, "pageviews.404.json"), status=404
    )

    result = resolve_articles.parse_volume_response(
        response,
        expected_project="en.wikipedia",
        expected_article="Intermittent_fasting",
        start=VOLUME_START,
        end=VOLUME_END,
    )

    assert result == {
        "status": "unavailable",
        "total_views": None,
        "window": {"start": "2026-08-26", "end": "2026-09-24"},
        "observed_days": None,
        "last_observed_date": None,
        "low_volume": None,
        "reason": "aqs_404_zero_or_not_loaded",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project", "pl.wikipedia"),
        ("article", "Other_article"),
        ("access", "desktop"),
        ("agent", "spider"),
        ("granularity", "monthly"),
    ],
    ids=[
        "wrong_project",
        "wrong_article",
        "wrong_access",
        "wrong_agent",
        "wrong_granularity",
    ],
)
def test_wrong_volume_identity_fails_before_candidate_mutation(field, value):
    assert hasattr(resolve_articles, "parse_volume_response")
    candidate = {"volume": {"status": "sentinel"}}
    items = [
        _volume_item(timestamp="2026082600", views=999),
        {**_volume_item(timestamp="2026082700"), field: value},
    ]

    with pytest.raises(resolve_articles.ResolveVolumeError, match=field):
        resolve_articles.parse_volume_response(
            _volume_response({"items": items}),
            expected_project="en.wikipedia",
            expected_article="Intermittent_fasting",
            start=VOLUME_START,
            end=VOLUME_END,
        )

    assert candidate["volume"] == {"status": "sentinel"}


@pytest.mark.parametrize(
    ("items", "message"),
    [
        (
            [
                _volume_item(timestamp="2026082600"),
                _volume_item(timestamp="2026082600"),
            ],
            "duplicate",
        ),
        ([_volume_item(views=-1)], "views"),
        ([_volume_item(views=True)], "views"),
        ([_volume_item(timestamp="2026092500")], "outside"),
        ([_volume_item(timestamp="2026082500")], "outside"),
        ([_volume_item(timestamp="2026082623")], "timestamp"),
        ([_volume_item() for _ in range(31)], "30"),
        (["not-an-object"], "object"),
    ],
)
def test_malformed_volume_items_are_errors_not_low_volume(items, message):
    assert hasattr(resolve_articles, "parse_volume_response")

    with pytest.raises(resolve_articles.ResolveVolumeError, match=message):
        resolve_articles.parse_volume_response(
            _volume_response({"items": items}),
            expected_project="en.wikipedia",
            expected_article="Intermittent_fasting",
            start=VOLUME_START,
            end=VOLUME_END,
        )


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_volume_http_failures_are_errors_not_low_volume(status):
    assert hasattr(resolve_articles, "parse_volume_response")

    with pytest.raises(resolve_articles.ResolveVolumeError, match=str(status)):
        resolve_articles.parse_volume_response(
            _volume_response({}, status=status),
            expected_project="en.wikipedia",
            expected_article="Intermittent_fasting",
            start=VOLUME_START,
            end=VOLUME_END,
        )


@pytest.mark.parametrize("body", [b"not-json", b"[]", b'{"items":{}}'])
def test_malformed_volume_json_is_an_error(body):
    with pytest.raises(resolve_articles.ResolveVolumeError, match="AQS"):
        resolve_articles.parse_volume_response(
            _volume_response(body),
            expected_project="en.wikipedia",
            expected_article="Intermittent_fasting",
            start=VOLUME_START,
            end=VOLUME_END,
        )


def test_volume_transport_failure_is_not_published_as_low_volume(
    tmp_path, monkeypatch, transport_stub, resolve_fixture
):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(resolve_articles, "sleep", lambda _seconds: None, raising=False)
    stub = transport_stub(
        [
            (
                200,
                {},
                _response_body(resolve_fixture, "resolve.search.en.wikipedia.json"),
            ),
            (
                200,
                {},
                _response_body(resolve_fixture, "resolve.normalized.en.wikipedia.json"),
            ),
        ]
    )

    def fail_volume(url, headers, timeout):
        stub.calls.append((url, dict(headers)))
        raise common.TransportError("offline transport failure")

    original_factory = stub

    def scripted_then_fail(url, headers, timeout):
        if url.startswith("https://wikimedia.org/api/rest_v1/"):
            return fail_volume(url, headers, timeout)
        return original_factory(url, headers, timeout)

    out = tmp_path / "resolved.json"
    assert resolve_articles.main(
        _args(out), transport=scripted_then_fail, today_utc=TODAY
    ) == 1
    assert out.exists()
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["status"] == "error"
    assert document["projects"][0]["error"]["code"] == "transport_error"
    assert "offline transport failure" in document["projects"][0]["error"]["message"]
    assert len(original_factory.calls) == 5


def test_low_volume_999_and_404_candidates_remain_selectable(
    tmp_path, monkeypatch, transport_stub, resolve_fixture
):
    _configure(monkeypatch, tmp_path)

    for index, (response, expected_status, expected_total) in enumerate(
        [
            (
                _volume_response({"items": [_volume_item(views=999)]}),
                "available",
                999,
            ),
            (
                _volume_response(
                    _response_body(resolve_fixture, "pageviews.404.json"),
                    status=404,
                ),
                "unavailable",
                None,
            ),
        ]
    ):
        out = tmp_path / f"resolved-{index}.json"
        stub = transport_stub(
            [
                (
                    200,
                    {},
                    _response_body(resolve_fixture, "resolve.search.en.wikipedia.json"),
                ),
                (
                    200,
                    {},
                    _response_body(resolve_fixture, "resolve.normalized.en.wikipedia.json"),
                ),
                (response.status, response.headers, response.body),
            ]
        )

        assert resolve_articles.main(
            [*_args(out), "--ttl-hours", "0"], transport=stub, today_utc=TODAY
        ) == 0

        document = json.loads(out.read_text(encoding="utf-8"))
        candidate = document["projects"][0]["candidates"][0]
        assert candidate["status"] == "selectable"
        assert candidate["volume"]["status"] == expected_status
        assert candidate["volume"]["total_views"] == expected_total
        if expected_status == "available":
            assert candidate["volume"]["low_volume"] is True
        else:
            assert candidate["volume"]["low_volume"] is None


def _volume_body_for_project(resolve_fixture, project: str, article: str) -> bytes:
    template = json.loads(
        _response_body(resolve_fixture, "pageviews.200.json").decode("utf-8")
    )
    source = template["items"][0]
    items: list[dict[str, object]] = []
    current = VOLUME_START
    while current <= VOLUME_END:
        items.append(
            {
                **source,
                "project": project,
                "article": article,
                "timestamp": f"{current:%Y%m%d}00",
                "views": 20,
            }
        )
        current += timedelta(days=1)
    return json.dumps({"items": items}, ensure_ascii=False).encode("utf-8")


def test_assignment_and_conservative_recommendation_contract():
    assert hasattr(resolve_articles, "parse_assignment")
    assert hasattr(resolve_articles, "recommend_candidates")
    assert resolve_articles.parse_assignment(" pl.wikipedia = Post przerywany ", "topic-for") == (
        "pl.wikipedia",
        "Post przerywany",
    )
    assert resolve_articles.recommend_candidates(
        [
            {"status": "selectable", "article": "Lower", "exact_title_match": False},
            {"status": "selectable", "article": "Exact", "exact_title_match": True},
            {"status": "selectable", "article": "Higher", "exact_title_match": False},
        ]
    ) == "Exact"
    assert resolve_articles.recommend_candidates(
        [
            {"status": "selectable", "article": "One", "exact_title_match": False},
            {"status": "selectable", "article": "Two", "exact_title_match": False},
        ]
    ) is None


def test_preflight_aggregates_over_fanout_and_bad_assignments_before_side_effects(
    tmp_path, monkeypatch, transport_stub
):
    def forbidden_user_agent() -> str:
        raise AssertionError("user_agent must not run before preflight completes")

    monkeypatch.setattr(common, "user_agent", forbidden_user_agent)
    stub = transport_stub([])
    out = tmp_path / "resolved.json"
    projects = ["en.wikipedia"] * 9
    args = [
        "--topic", "topic",
        *[arg for project in projects for arg in ("--projects", project)],
        "--topic-for", "en.wikipedia=one",
        "--topic-for", "en.wikipedia=two",
        "--topic-for", "https://en.wikipedia.org/w/api.php=three",
        "--select", "en.wikipedia=https://en.wikipedia.org/wiki/Target",
        "--out", str(out),
    ]
    assert resolve_articles.main(args, transport=stub) == 2
    assert stub.calls == []
    assert not out.exists()


def test_multi_project_override_is_scoped_and_provenance_is_persisted(
    tmp_path, monkeypatch, transport_stub, resolve_fixture
):
    _configure(monkeypatch, tmp_path)
    out = tmp_path / "resolved.json"
    stub = transport_stub(
        [
            (200, {}, _response_body(resolve_fixture, "resolve.search.en.wikipedia.json")),
            (200, {}, _response_body(resolve_fixture, "resolve.normalized.en.wikipedia.json")),
            (200, {}, _volume_body_for_project(resolve_fixture, "en.wikipedia", "Intermittent_fasting")),
            (200, {}, _response_body(resolve_fixture, "resolve.search.en.wikipedia.json")),
            (200, {}, _response_body(resolve_fixture, "resolve.normalized.en.wikipedia.json")),
            (200, {}, _volume_body_for_project(resolve_fixture, "pl.wikipedia", "Intermittent_fasting")),
        ]
    )
    args = [
        "--topic", "intermittent fasting",
        "--projects", "en.wikipedia", "--projects", "pl.wikipedia",
        "--topic-for", "pl.wikipedia=Post przerywany",
        "--out", str(out),
    ]
    assert resolve_articles.main(args, transport=stub, today_utc=TODAY) == 0
    assert [urlparse(call[0]).netloc for call in stub.calls] == [
        "en.wikipedia.org", "en.wikipedia.org", "wikimedia.org",
        "pl.wikipedia.org", "pl.wikipedia.org", "wikimedia.org",
    ]
    assert parse_qs(urlparse(stub.calls[0][0]).query)["srsearch"] == ["intermittent fasting"]
    assert parse_qs(urlparse(stub.calls[3][0]).query)["srsearch"] == ["Post przerywany"]
    document = json.loads(out.read_text(encoding="utf-8"))
    assert [project["project"] for project in document["projects"]] == [
        "en.wikipedia", "pl.wikipedia"
    ]
    assert document["projects"][0]["query_source"] == "shared"
    assert document["projects"][1]["query_source"] == "project_override"
    assert document["projects"][1]["effective_query"] == "Post przerywany"


def test_aggregate_project_outcomes_precedence_and_records():
    assert hasattr(resolve_articles, "aggregate_project_outcomes")
    ready = {"project": "en.wikipedia", "status": "ready", "candidates": [{"article": "Ready"}]}
    ambiguous = {"project": "pl.wikipedia", "status": "ambiguous", "candidates": [{"article": "Ambiguous"}]}
    error = {"project": "de.wikipedia", "status": "error", "search_hits": None, "error": {"code": "x", "message": "x"}}
    unresolved = {
        "project": "fr.wikipedia", "status": "unresolved", "search_hits": [{"title": "X", "rank": 1}],
        "candidates": [{"status": "unresolved", "reason": "missing_target"}], "reason": "no_selectable_candidates",
    }
    assert resolve_articles.aggregate_project_outcomes([ready, ambiguous]) == ("awaiting_confirmation", 0)
    assert resolve_articles.aggregate_project_outcomes([ready, error]) == ("partial_error", 3)
    assert resolve_articles.aggregate_project_outcomes([ready, error, unresolved]) == ("unresolved", 2)
    assert resolve_articles.aggregate_project_outcomes([error]) == ("error", 1)


def test_project_failures_are_aggregated_and_confirmation_rejects_partial_bytes(
    tmp_path, monkeypatch, transport_stub, resolve_fixture, forbidden_transport
):
    _configure(monkeypatch, tmp_path)
    out = tmp_path / "resolved.json"
    search = _response_body(resolve_fixture, "resolve.search.en.wikipedia.json")
    metadata = _response_body(resolve_fixture, "resolve.normalized.en.wikipedia.json")
    stub = transport_stub(
        [
            (200, {}, search), (200, {}, metadata),
            (200, {}, _volume_body_for_project(resolve_fixture, "en.wikipedia", "Intermittent_fasting")),
            (200, {}, b'{"error":{"code":"badrequest","info":"raw body must not persist"}}'),
        ]
    )
    args = [
        "--topic", "intermittent fasting",
        "--projects", "en.wikipedia", "--projects", "pl.wikipedia",
        "--out", str(out),
    ]
    assert resolve_articles.main(args, transport=stub, today_utc=TODAY) == 3
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["status"] == "partial_error"
    assert document["projects"][0]["status"] == "ready"
    assert document["projects"][1]["status"] == "error"
    assert document["projects"][1]["error"]["code"] == "action_api_error"
    assert "raw body" not in json.dumps(document["projects"][1]["error"])
    before = out.read_bytes()
    _forbid_confirmation_side_effects(monkeypatch)
    assert resolve_articles.main(
        [*args, "--select", "en.wikipedia=Intermittent_fasting", "--select", "pl.wikipedia=Intermittent_fasting"],
        transport=forbidden_transport,
    ) == 2
    assert out.read_bytes() == before


def test_zero_hit_and_all_structural_invalid_projects_remain_unresolved(
    tmp_path, monkeypatch, transport_stub
):
    _configure(monkeypatch, tmp_path)
    out = tmp_path / "resolved.json"
    metadata = _metadata_payload(
        redirects=[{"from": "Requested", "to": "Category:Target"}],
        pages=[{"ns": 14, "title": "Category:Target"}],
    )
    stub = transport_stub(
        [
            (200, {}, b'{"query":{"search":[]}}'),
            (200, {}, b'{"query":{"search":[{"ns":0,"title":"Requested"}]}}'),
            (200, {}, metadata),
        ]
    )
    args = [
        "--topic", "topic", "--projects", "en.wikipedia", "--projects", "pl.wikipedia", "--out", str(out)
    ]
    assert resolve_articles.main(args, transport=stub, today_utc=TODAY) == 2
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["status"] == "unresolved"
    assert document["projects"][0]["search_hits"] == []
    assert document["projects"][0]["reason"] == "search_no_hits"
    assert document["projects"][1]["search_hits"] == [{"title": "Requested", "rank": 1}]
    assert document["projects"][1]["candidates"][0]["status"] == "unresolved"
    assert document["projects"][1]["candidates"][0]["reason"] == "non_article_namespace"
    assert document["projects"][1]["reason"] == "no_selectable_candidates"


def test_preflight_reports_all_input_classes_in_one_error():
    with pytest.raises(resolve_articles.ResolveInputError) as raised:
        resolve_articles.preflight_inputs(
            "",
            ["en.wikipedia", "en.wikipedia", "unknown.example", None],  # type: ignore[list-item]
            ["en.wikipedia", "en.wikipedia=one", "en.wikipedia=two", None, "pl.wikipedia=missing"],  # type: ignore[list-item]
            ["en.wikipedia=Foo", "en.wikipedia=Bar", None],  # type: ignore[list-item]
        )
    message = str(raised.value)
    for expected in (
        "topic must be a non-empty string",
        "duplicate project",
        "committed allowlist",
        "must be a string",
        "must use PROJECT=VALUE",
        "duplicate topic-for",
        "not requested",
    ):
        assert expected in message


def test_ambiguous_discovery_requires_reason_without_mutating_discovery(
    tmp_path, monkeypatch, transport_stub, resolve_fixture, forbidden_transport
):
    _configure(monkeypatch, tmp_path)
    out = tmp_path / "resolved.json"
    search = b'{"query":{"search":[{"ns":0,"title":"Meaning one"},{"ns":0,"title":"Meaning two"}]}}'
    metadata = _metadata_payload(
        pages=[
            {"ns": 0, "title": "Meaning one", "pageprops": {}},
            {"ns": 0, "title": "Meaning two", "pageprops": {}},
        ]
    )
    stub = transport_stub(
        [
            (200, {}, search),
            (200, {}, metadata),
            (200, {}, _volume_body_for_project(resolve_fixture, "en.wikipedia", "Meaning_one")),
            (200, {}, _volume_body_for_project(resolve_fixture, "en.wikipedia", "Meaning_two")),
        ]
    )
    args = ["--topic", "topic", "--projects", "en.wikipedia", "--out", str(out)]
    assert resolve_articles.main(args, transport=stub, today_utc=TODAY) == 0
    discovery = json.loads(out.read_text(encoding="utf-8"))
    assert discovery["status"] == "awaiting_confirmation"
    assert discovery["projects"][0]["status"] == "ambiguous"
    assert discovery["projects"][0]["recommendation"] is None
    before = out.read_bytes()
    _forbid_confirmation_side_effects(monkeypatch)
    assert resolve_articles.main(
        [*args, "--select", "en.wikipedia=Meaning_one"],
        transport=forbidden_transport,
    ) == 2
    assert out.read_bytes() == before
    assert resolve_articles.main(
        [*args, "--select", "en.wikipedia=Meaning_one", "--reason", "exact user concept"],
        transport=forbidden_transport,
    ) == 0
    confirmed = json.loads(out.read_text(encoding="utf-8"))
    assert confirmed["projects"][0]["selection"]["reason"] == "exact user concept"


def test_all_error_and_unresolved_precedence_manifests_preserve_order(
    tmp_path, monkeypatch, transport_stub, resolve_fixture
):
    _configure(monkeypatch, tmp_path)
    out = tmp_path / "mixed.json"
    search = _response_body(resolve_fixture, "resolve.search.en.wikipedia.json")
    metadata = _response_body(resolve_fixture, "resolve.normalized.en.wikipedia.json")
    api_error = b'{"error":{"code":"badrequest","info":"do not persist"}}'
    stub = transport_stub(
        [
            (200, {}, search), (200, {}, metadata),
            (200, {}, _volume_body_for_project(resolve_fixture, "en.wikipedia", "Intermittent_fasting")),
            (200, {}, b'{"query":{"search":[]}}'),
            (200, {}, api_error),
        ]
    )
    args = [
        "--topic", "topic",
        "--projects", "en.wikipedia", "--projects", "pl.wikipedia", "--projects", "de.wikipedia",
        "--out", str(out),
    ]
    assert resolve_articles.main(args, transport=stub, today_utc=TODAY) == 2
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["status"] == "unresolved"
    assert [record["project"] for record in document["projects"]] == [
        "en.wikipedia", "pl.wikipedia", "de.wikipedia"
    ]
    assert [record["status"] for record in document["projects"]] == [
        "ready", "unresolved", "error"
    ]
    assert document["projects"][1]["reason"] == "search_no_hits"
    assert document["projects"][2]["error"]["code"] == "action_api_error"

    all_error = tmp_path / "all-error.json"
    stub = transport_stub([(200, {}, api_error), (200, {}, api_error)])
    all_args = [
        "--topic", "topic", "--projects", "en.wikipedia", "--projects", "pl.wikipedia",
        "--out", str(all_error),
    ]
    assert resolve_articles.main(
        [*all_args, "--ttl-hours", "0"], transport=stub, today_utc=TODAY
    ) == 1
    all_document = json.loads(all_error.read_text(encoding="utf-8"))
    assert all_document["status"] == "error"
    assert all(record["error"]["code"] == "action_api_error" for record in all_document["projects"])


def test_confirmation_rejects_unresolved_and_error_manifests(
    tmp_path, monkeypatch, transport_stub, resolve_fixture, forbidden_transport
):
    _configure(monkeypatch, tmp_path)
    for status in ("unresolved", "partial_error", "error"):
        out = tmp_path / f"{status}.json"
        document = {
            "contract_version": resolve_articles.CONTRACT_VERSION,
            "run_mode": "discover",
            "status": status,
            "topic": "topic",
            "projects": [],
        }
        common.dump_json(document, out)
        before = out.read_bytes()
        _forbid_confirmation_side_effects(monkeypatch)
    assert resolve_articles.main(
        [
            "--topic", "topic", "--projects", "en.wikipedia", "--out", str(out),
            "--select", "en.wikipedia=Target",
        ],
        transport=forbidden_transport,
    ) == 2
    assert out.read_bytes() == before


def test_ttl_parser_uses_24_environment_fallback_and_cli_precedence(
    tmp_path, monkeypatch
):
    args = _args(tmp_path / "resolved.json")
    monkeypatch.delenv("WTI_TTL_HOURS", raising=False)
    assert resolve_articles._parser().parse_args(args).ttl_hours == 24.0

    monkeypatch.setenv("WTI_TTL_HOURS", "6.5")
    assert resolve_articles._parser().parse_args(args).ttl_hours == 6.5
    assert (
        resolve_articles._parser().parse_args([*args, "--ttl-hours", "0"]).ttl_hours
        == 0.0
    )

    monkeypatch.setenv("WTI_TTL_HOURS", "not-a-number")
    assert resolve_articles._parser().parse_args(args).ttl_hours == 24.0


@pytest.mark.parametrize("value", ["-1", "nan", "inf", "-inf"])
def test_invalid_ttl_fails_before_user_agent_transport_or_output(
    value, tmp_path, monkeypatch, transport_stub
):
    def forbidden_user_agent() -> str:
        raise AssertionError("invalid TTL must fail before User-Agent work")

    monkeypatch.setattr(common, "user_agent", forbidden_user_agent)
    out = tmp_path / "resolved.json"
    stub = transport_stub([])

    with pytest.raises(SystemExit) as raised:
        resolve_articles.main(
            [*_args(out), "--ttl-hours", value], transport=stub
        )

    assert raised.value.code == 2
    assert stub.calls == []
    assert not out.exists()


def test_warm_validated_discovery_cache_makes_zero_transport_calls(
    tmp_path, monkeypatch, transport_stub, resolve_fixture
):
    _configure(monkeypatch, tmp_path)
    out = tmp_path / "resolved.json"
    cold = transport_stub(
        [
            (200, {}, _response_body(resolve_fixture, "resolve.search.en.wikipedia.json")),
            (200, {}, _response_body(resolve_fixture, "resolve.normalized.en.wikipedia.json")),
            (200, {}, _volume_body(resolve_fixture)),
        ]
    )
    assert resolve_articles.main(_args(out), transport=cold, today_utc=TODAY) == 0
    cold_projects = json.loads(out.read_text(encoding="utf-8"))["projects"]

    warm = transport_stub([])
    assert resolve_articles.main(_args(out), transport=warm, today_utc=TODAY) == 0

    assert warm.calls == []
    assert json.loads(out.read_text(encoding="utf-8"))["projects"] == cold_projects


def test_ttl_zero_bypasses_fresh_cache_and_rewrites_validated_entries(
    tmp_path, monkeypatch, transport_stub, resolve_fixture
):
    _configure(monkeypatch, tmp_path)
    out = tmp_path / "resolved.json"
    responses = [
        (200, {}, _response_body(resolve_fixture, "resolve.search.en.wikipedia.json")),
        (200, {}, _response_body(resolve_fixture, "resolve.normalized.en.wikipedia.json")),
        (200, {}, _volume_body(resolve_fixture)),
    ]
    assert resolve_articles.main(
        _args(out), transport=transport_stub(responses), today_utc=TODAY
    ) == 0
    cache_before = {
        path.name: path.read_bytes() for path in (tmp_path / "cache").glob("*.json")
    }
    assert len(cache_before) == 3

    forced = transport_stub(responses)
    assert resolve_articles.main(
        [*_args(out), "--ttl-hours", "0"],
        transport=forced,
        today_utc=TODAY,
    ) == 0

    assert len(forced.calls) == 3
    assert len(list((tmp_path / "cache").glob("*.json"))) == 3


def test_retry_after_is_honored_with_three_attempt_ceiling(
    tmp_path, monkeypatch, transport_stub, resolve_fixture, capsys
):
    _configure(monkeypatch, tmp_path)
    sleeps: list[float] = []
    monkeypatch.setattr(resolve_articles, "sleep", sleeps.append, raising=False)
    stub = transport_stub(
        [
            (429, {"Retry-After": "2"}, b"rate limited"),
            (429, {"Retry-After": "3"}, b"rate limited"),
            (200, {}, _response_body(resolve_fixture, "resolve.search.en.wikipedia.json")),
            (200, {}, _response_body(resolve_fixture, "resolve.normalized.en.wikipedia.json")),
            (200, {}, _volume_body(resolve_fixture)),
        ]
    )

    assert resolve_articles.main(_args(tmp_path / "resolved.json"), transport=stub, today_utc=TODAY) == 0

    diagnostics = capsys.readouterr().err
    assert len(stub.calls) == 5
    assert sleeps == [2.0, 3.0]
    assert "retry 1/3 after 2.0 seconds" in diagnostics
    assert "retry 2/3 after 3.0 seconds" in diagnostics


def test_retryable_failure_exhausts_three_calls_without_cache(
    tmp_path, monkeypatch, transport_stub
):
    _configure(monkeypatch, tmp_path)
    sleeps: list[float] = []
    monkeypatch.setattr(resolve_articles, "sleep", sleeps.append, raising=False)
    stub = transport_stub([(503, {}, b"down") for _ in range(3)])

    assert resolve_articles.main(_args(tmp_path / "resolved.json"), transport=stub) == 1

    assert len(stub.calls) == 3
    assert sleeps == [5.0, 5.0]
    assert list((tmp_path / "cache").glob("*.json")) == []


def test_403_is_immediately_fatal_without_retry_or_cache(
    tmp_path, monkeypatch, transport_stub
):
    _configure(monkeypatch, tmp_path)
    sleeps: list[float] = []
    monkeypatch.setattr(resolve_articles, "sleep", sleeps.append, raising=False)
    stub = transport_stub([(403, {}, b"forbidden")])

    assert resolve_articles.main(_args(tmp_path / "resolved.json"), transport=stub) == 1

    assert len(stub.calls) == 1
    assert sleeps == []
    assert list((tmp_path / "cache").glob("*.json")) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project", "pl.wikipedia"),
        ("article", "Other_article"),
        ("access", "desktop"),
        ("agent", "spider"),
        ("granularity", "monthly"),
    ],
)
def test_wrong_aqs_identity_creates_no_aqs_cache_or_candidate_volume(
    field,
    value,
    tmp_path,
    monkeypatch,
    transport_stub,
    resolve_fixture,
):
    _configure(monkeypatch, tmp_path)
    out = tmp_path / "resolved.json"
    payload = json.loads(_volume_body(resolve_fixture).decode("utf-8"))
    payload["items"][0][field] = value
    stub = transport_stub(
        [
            (200, {}, _response_body(resolve_fixture, "resolve.search.en.wikipedia.json")),
            (200, {}, _response_body(resolve_fixture, "resolve.normalized.en.wikipedia.json")),
            (200, {}, json.dumps(payload).encode("utf-8")),
        ]
    )

    assert resolve_articles.main(_args(out), transport=stub, today_utc=TODAY) == 1

    aqs_url = series_url(
        "en.wikipedia", "Intermittent_fasting", "20260826", "20260924"
    )
    assert not common.cache_path_for_key(common.cache_key_for_url(aqs_url)).exists()
    project = json.loads(out.read_text(encoding="utf-8"))["projects"][0]
    assert "volume" not in project["candidates"][0]


def test_declared_oversize_rejects_before_stream_read(monkeypatch):
    class DeclaredOversizeStream:
        status = 200
        headers = {"Content-Length": str(resolve_articles.MAX_RESPONSE_BYTES + 1)}
        reads: list[int | None] = []

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, size: int | None = -1) -> bytes:
            self.reads.append(size)
            return b"{}"

    stream = DeclaredOversizeStream()
    monkeypatch.setattr(common.urllib.request, "urlopen", lambda *_args, **_kwargs: stream)

    with pytest.raises(common.ResponseTooLarge):
        resolve_articles.bounded_transport("https://en.wikipedia.org/w/api.php", {})

    assert stream.reads == []


def _valid_discovery_document(
    out: Path, tmp_path: Path, monkeypatch, transport_stub, resolve_fixture
) -> dict[str, object]:
    _run_discovery(out, tmp_path, monkeypatch, transport_stub, resolve_fixture)
    return json.loads(out.read_text(encoding="utf-8"))


def test_validate_resolved_document_accepts_discovery_and_confirmed_documents(
    tmp_path, monkeypatch, transport_stub, resolve_fixture, forbidden_transport
):
    assert hasattr(resolve_articles, "validate_resolved_document")
    out = tmp_path / "resolved.json"
    discovery = _valid_discovery_document(
        out, tmp_path, monkeypatch, transport_stub, resolve_fixture
    )
    _forbid_confirmation_side_effects(monkeypatch)

    resolve_articles.validate_resolved_document(
        discovery,
        expected_topic="intermittent fasting",
        expected_projects=["en.wikipedia"],
        expected_overrides=[],
        mode="discover",
    )
    confirmed, exit_code = resolve_articles.confirm(
        topic="intermittent fasting",
        projects=["en.wikipedia"],
        topic_overrides=[],
        selections={"en.wikipedia": "Intermittent_fasting"},
        reason=None,
        out=out,
    )
    assert exit_code == 0
    assert confirmed is not None
    resolve_articles.validate_resolved_document(
        confirmed,
        expected_topic="intermittent fasting",
        expected_projects=["en.wikipedia"],
        expected_overrides=[],
        mode="confirm",
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "delete_candidate_provenance",
        "selectable_candidate_reason",
        "search_hits_null_without_error",
        "structural_unresolved_without_records",
        "zero_hit_with_candidates",
        "available_volume_null_total",
        "unavailable_volume_numeric_total",
        "changed_override_expression",
    ],
)
def test_validate_resolved_document_rejects_structural_mutations(
    mutation,
    tmp_path,
    monkeypatch,
    transport_stub,
    resolve_fixture,
):
    out = tmp_path / "resolved.json"
    document = _valid_discovery_document(
        out, tmp_path, monkeypatch, transport_stub, resolve_fixture
    )
    project = document["projects"][0]
    candidate = project["candidates"][0]

    if mutation == "delete_candidate_provenance":
        del candidate["redirect_chain"]
    elif mutation == "selectable_candidate_reason":
        candidate["reason"] = "redirect_cycle"
    elif mutation == "search_hits_null_without_error":
        project["search_hits"] = None
    elif mutation == "structural_unresolved_without_records":
        project["status"] = "unresolved"
        project["reason"] = "no_selectable_candidates"
        project["candidates"] = []
    elif mutation == "zero_hit_with_candidates":
        project["status"] = "unresolved"
        project["reason"] = "search_no_hits"
    elif mutation == "available_volume_null_total":
        candidate["volume"]["total_views"] = None
    elif mutation == "unavailable_volume_numeric_total":
        candidate["volume"] = {
            "status": "unavailable",
            "total_views": 0,
            "window": {"start": "2026-08-26", "end": "2026-09-24"},
            "observed_days": None,
            "last_observed_date": None,
            "low_volume": None,
            "reason": "aqs_404_zero_or_not_loaded",
        }
    else:
        document["topic_overrides"] = [
            {"project": "en.wikipedia", "query": "changed"}
        ]

    with pytest.raises(resolve_articles.ResolveInputError):
        resolve_articles.validate_resolved_document(
            document,
            expected_topic="intermittent fasting",
            expected_projects=["en.wikipedia"],
            expected_overrides=[],
            mode="discover",
        )


def test_override_context_change_and_omission_follow_cli_path_without_side_effects(
    tmp_path, monkeypatch, transport_stub, resolve_fixture, forbidden_transport
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
    forbidden_calls: list[str] = []

    def record(name):
        def forbidden(*_args: object, **_kwargs: object) -> None:
            forbidden_calls.append(name)
            raise AssertionError(f"confirmation must not call {name}")

        return forbidden

    for name in ("user_agent", "throttle", "read_json_cache", "write_json_cache"):
        monkeypatch.setattr(common, name, record(name))

    changed = [
        *_args(out),
        "--topic-for",
        "en.wikipedia=changed",
        "--select",
        "en.wikipedia=Intermittent_fasting",
    ]
    omitted = [*_args(out), "--select", "en.wikipedia=Intermittent_fasting"]
    assert resolve_articles.main(changed, transport=forbidden_transport) == 2
    assert out.read_bytes() == before
    assert resolve_articles.main(omitted, transport=forbidden_transport) == 2
    assert out.read_bytes() == before
    assert forbidden_calls == []

    exact_replay = [
        *_args(out),
        "--topic-for",
        "en.wikipedia=fasting",
        "--select",
        "en.wikipedia=Intermittent_fasting",
    ]
    assert resolve_articles.main(exact_replay, transport=forbidden_transport) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["status"] == "confirmed"


def test_structural_unresolved_manifest_cannot_be_confirmed_and_keeps_bytes(
    tmp_path, monkeypatch, transport_stub, resolve_fixture, forbidden_transport
):
    out = tmp_path / "resolved.json"
    document = _valid_discovery_document(
        out, tmp_path, monkeypatch, transport_stub, resolve_fixture
    )
    project = document["projects"][0]
    project["status"] = "unresolved"
    project["reason"] = "no_selectable_candidates"
    project["candidates"] = []
    common.dump_json(document, out)
    before = out.read_bytes()
    _forbid_confirmation_side_effects(monkeypatch)

    assert resolve_articles.main(
        [*_args(out), "--select", "en.wikipedia=Intermittent_fasting"],
        transport=forbidden_transport,
    ) == 2
    assert out.read_bytes() == before


@pytest.mark.parametrize("failure", ["dump_json", "os_replace"])
def test_publication_failure_preserves_bytes_and_removes_staging_file(
    failure,
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

    if failure == "dump_json":
        def fail_publish(_document, _path):
            raise OSError("injected publication failure")

        monkeypatch.setattr(common, "dump_json", fail_publish)
    else:
        def fail_replace(_source, _target):
            raise OSError("injected replace failure")

        monkeypatch.setattr(common.os, "replace", fail_replace)

    assert resolve_articles.main(
        [*_args(out), "--select", "en.wikipedia=Intermittent_fasting"],
        transport=forbidden_transport,
    ) == 1
    assert out.read_bytes() == before
    assert list(out.parent.glob(".resolved.json.*.tmp")) == []


def test_oversized_response_becomes_bounded_project_error_without_cache(
    tmp_path, monkeypatch, transport_stub, resolve_fixture
):
    _configure(monkeypatch, tmp_path)
    out = tmp_path / "resolved.json"
    stub = transport_stub(
        [
            (200, {}, _response_body(resolve_fixture, "resolve.search.en.wikipedia.json")),
            (200, {}, _response_body(resolve_fixture, "resolve.normalized.en.wikipedia.json")),
        ]
    )

    def oversized(url, headers, timeout):
        if url.startswith("https://wikimedia.org/api/rest_v1/"):
            raise common.ResponseTooLarge("resolver response exceeds byte limit")
        return stub(url, headers, timeout)

    assert resolve_articles.main(_args(out), transport=oversized, today_utc=TODAY) == 1

    project = json.loads(out.read_text(encoding="utf-8"))["projects"][0]
    assert project["error"]["code"] == "response_too_large"
    aqs_url = series_url(
        "en.wikipedia", "Intermittent_fasting", "20260826", "20260924"
    )
    assert not common.cache_path_for_key(common.cache_key_for_url(aqs_url)).exists()

# --- 04-07 live evidence: the real captures agree with the production parser --


def test_live_redirect_chain_evidence_resolves_to_the_final_namespace_0_target(
    resolve_fixture,
):
    """Real 2026-09-25 capture: `Barack obama` and `Obama` both forward to
    `Barack Obama`, and only the final namespace-0 target reaches AQS."""
    body = _response_body(resolve_fixture, "resolve.redirects.en.wikipedia.json")
    payload = json.loads(body.decode("utf-8"))
    assert [(r["from"], r["to"]) for r in payload["query"]["redirects"]] == [
        ("Barack obama", "Barack Obama"),
        ("Obama", "Barack Obama"),
    ]
    assert all(p["ns"] == 0 for p in payload["query"]["pages"])

    candidates = resolve_articles.parse_metadata(
        body,
        [{"title": "Barack obama", "rank": 1}],
        "Barack obama",
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["status"] == "selectable"
    assert candidate["article"] == "Barack_Obama"
    assert candidate["title"] == "Barack Obama"
    assert candidate["redirect_chain"] == ["Barack Obama"]
    assert candidate["disambiguation"] is False


def test_live_disambiguation_evidence_carries_the_pageprops_key(resolve_fixture):
    """The key is present with an empty-string value, so presence must be tested
    by membership, never by truthiness."""
    body = _response_body(resolve_fixture, "resolve.disambiguation.en.wikipedia.json")
    payload = json.loads(body.decode("utf-8"))
    props = payload["query"]["pages"][0]["pageprops"]
    assert "disambiguation" in props
    assert props["disambiguation"] == ""

    candidates = resolve_articles.parse_metadata(
        body, [{"title": "Mercury", "rank": 1}], "Mercury"
    )

    assert candidates[0]["status"] == "unresolved"
    assert candidates[0]["disambiguation"] is True
    assert candidates[0]["reason"] == "disambiguation_page"


def test_live_http_200_error_envelope_is_rejected_as_a_success_body(resolve_fixture):
    body = _response_body(resolve_fixture, "resolve.api-error.en.wikipedia.json")
    payload = json.loads(body.decode("utf-8"))
    assert payload["errors"][0]["code"] == "search-title-disabled"

    with pytest.raises(resolve_articles.ResolveApiError, match="action_api_error"):
        resolve_articles.parse_action_payload(body, "query")
    with pytest.raises(resolve_articles.ResolveApiError, match="action_api_error"):
        resolve_articles.parse_metadata(body, [{"title": "x", "rank": 1}], "x")


def test_live_batched_evidence_yields_five_ordered_namespace_0_candidates(
    resolve_fixture,
):
    """Real `srlimit=5` behavior: five distinct ns=0 pages come back.

    The recommendation is still unambiguous, but only through the second clause
    of the policy -- exactly one candidate is a direct exact-title match for the
    effective query, while the other four are merely selectable.
    """
    search = json.loads(
        _response_body(resolve_fixture, "resolve.search.batched.en.wikipedia.json")
        .decode("utf-8")
    )
    hits = resolve_articles._search_hits(search)
    assert len(hits) == resolve_articles.MAX_SEARCH_RESULTS
    assert [h["rank"] for h in hits] == [1, 2, 3, 4, 5]
    assert hits[0]["title"] == "Intermittent fasting"

    metadata = _response_body(
        resolve_fixture, "resolve.normalized.batched.en.wikipedia.json"
    )
    candidates = resolve_articles.parse_metadata(metadata, hits, "intermittent fasting")

    assert [c["title"] for c in candidates] == [
        "Intermittent fasting", "Fasting", "Michael Mosley", "Fastic", "Greg O'Gallagher",
    ]
    assert all(c["status"] == "selectable" for c in candidates)
    exact = [c for c in candidates if c["exact_title_match"] is True]
    assert [c["title"] for c in exact] == ["Intermittent fasting"]
    # Five selectables is not a single recommendation; only the exact-title
    # clause resolves it. With two exact matches the policy must decline.
    assert resolve_articles.recommend_candidates(candidates) == "Intermittent_fasting"
    assert resolve_articles.recommend_candidates([*candidates, dict(candidates[0])]) is None


def test_live_30d_pageviews_evidence_validates_and_sums_exactly(resolve_fixture):
    """The 2026-09-25 capture: 30 complete days, 413877 views, no zero-fill."""
    body = _response_body(resolve_fixture, "resolve.pageviews.30d.en.wikipedia.json")
    start, end = date(2026, 8, 26), date(2026, 9, 24)
    assert (end - start).days + 1 == resolve_articles.VOLUME_DAYS

    evidence = resolve_articles.parse_volume_response(
        common.TransportResponse(status=200, headers={}, body=body),
        expected_project="en.wikipedia",
        expected_article="Barack_Obama",
        start=start,
        end=end,
    )

    assert evidence["status"] == "available"
    assert evidence["total_views"] == 413877
    assert evidence["observed_days"] == 30
    assert evidence["last_observed_date"] == "2026-09-24"
    assert evidence["low_volume"] is False
    # The recorded total must be independently reproducible from the items, and
    # no missing calendar day may be represented as a fabricated zero.
    items = json.loads(body.decode("utf-8"))["items"]
    assert len(items) == 30
    assert sum(i["views"] for i in items) == evidence["total_views"]
    assert {i["timestamp"][:8] for i in items} == {
        f"{(start + timedelta(days=offset)):%Y%m%d}" for offset in range(30)
    }
    assert all(i["views"] > 0 for i in items)
