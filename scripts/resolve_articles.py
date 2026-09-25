"""Resolve a natural-language topic to explicit Wikipedia article candidates.

Discovery performs serial Action API search, canonical title metadata, and a
30-complete-day AQS evidence check. Search output is never an implicit
selection: callers inspect the manifest and use the separate confirmation state
transition before downstream spec authoring.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from urllib.parse import quote, urlencode

import common
import fetch_pageviews
from common import Transport, TransportResponse

CONTRACT_VERSION = "resolved.v1"
MAX_RESPONSE_BYTES = 1_048_576
MAX_PROJECTS = 8
MAX_SEARCH_RESULTS = 5
MAX_REDIRECT_HOPS = 10
MAX_DISPLAY_CODE_POINTS = 256
VOLUME_DAYS = 30
PROJECTS_FILE = Path(__file__).resolve().parents[1] / "assets" / "wikipedia-projects.json"


class ResolveInputError(ValueError):
    """Resolver input failed offline validation."""


class ResolveResponseError(RuntimeError):
    """An upstream response did not match the required contract."""


class ResolveApiError(ResolveResponseError):
    """The Action API returned an error envelope inside HTTP 200."""


class ResolveVolumeError(ResolveResponseError):
    """An AQS response was unavailable or structurally invalid."""


def load_allowed_projects() -> frozenset[str]:
    """Load exact project codes from the committed versioned Site Matrix asset."""
    try:
        payload = json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ResolveInputError("project catalog is not valid UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise ResolveInputError("project catalog must be a JSON object")
    if payload.get("schema_version") != "wikipedia-projects.v1":
        raise ResolveInputError("project catalog has an unsupported schema_version")
    projects = payload.get("projects")
    if not isinstance(projects, list) or not projects:
        raise ResolveInputError("project catalog must contain a non-empty projects list")
    if any(not isinstance(project, str) or not project for project in projects):
        raise ResolveInputError("project catalog entries must be non-empty strings")
    if len(set(cast(list[str], projects))) != len(projects):
        raise ResolveInputError("project catalog contains duplicate project codes")
    return frozenset(cast(list[str], projects))


def bounded_transport(
    url: str,
    headers: dict[str, str],
    timeout: float = 30.0,
) -> TransportResponse:
    """Delegate to the shared transport with the resolver's hard byte ceiling."""
    return common.default_transport(url, headers, timeout, max_bytes=MAX_RESPONSE_BYTES)


def _clean_text(value: object, field: str) -> str:
    """Strip C0/C1 controls and cap external display strings by code points."""
    if not isinstance(value, str):
        raise ResolveResponseError(f"{field} must be a string")
    cleaned = "".join(
        character
        for character in value
        if not (0 <= ord(character) <= 0x1F or 0x7F <= ord(character) <= 0x9F)
    )[:MAX_DISPLAY_CODE_POINTS]
    if not cleaned:
        raise ResolveResponseError(f"{field} must not be empty")
    return cleaned


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResolveInputError(f"{field} must be a non-empty string")
    return value.strip()


def _split_expression(value: object, field: str) -> tuple[str, str]:
    if not isinstance(value, str):
        raise ResolveInputError(f"{field} must be a string")
    project, separator, expression = value.partition("=")
    project = project.strip()
    expression = expression.strip()
    if not separator or not project or not expression:
        raise ResolveInputError(f"{field} must use PROJECT=VALUE")
    return project, expression


def preflight_inputs(
    topic: str,
    projects: Sequence[str],
    topic_overrides: Sequence[str],
    selections: Sequence[str],
) -> tuple[list[str], list[tuple[str, str]], dict[str, str]]:
    """Validate all offline context and return normalized ordered collections."""
    errors: list[str] = []
    normalized_topic = ""
    if not isinstance(topic, str) or not topic.strip():
        errors.append("topic must be a non-empty string")
    else:
        normalized_topic = topic.strip()

    try:
        allowed = load_allowed_projects()
    except ResolveInputError as error:
        allowed = frozenset()
        errors.append(str(error))

    normalized_projects: list[str] = []
    seen_projects: set[str] = set()
    for index, project in enumerate(projects):
        if not isinstance(project, str):
            errors.append(f"projects[{index}] must be a string")
            continue
        candidate = project.strip()
        if not candidate:
            errors.append(f"projects[{index}] must be a non-empty project code")
            continue
        if candidate in seen_projects:
            errors.append(f"duplicate project: {candidate}")
            continue
        seen_projects.add(candidate)
        if candidate not in allowed:
            errors.append(f"project is not in the committed allowlist: {candidate}")
            continue
        normalized_projects.append(candidate)
    if not projects:
        errors.append("at least one project is required")
    if len(normalized_projects) > MAX_PROJECTS:
        errors.append(f"at most {MAX_PROJECTS} projects are allowed")

    normalized_overrides: list[tuple[str, str]] = []
    seen_overrides: set[str] = set()
    for index, expression in enumerate(topic_overrides):
        try:
            project, query = _split_expression(expression, f"topic-for[{index}]")
        except ResolveInputError as error:
            errors.append(str(error))
            continue
        if project not in seen_projects:
            errors.append(f"topic-for project is not requested: {project}")
            continue
        if project not in allowed:
            errors.append(f"topic-for project is not in the committed allowlist: {project}")
            continue
        if project in seen_overrides:
            errors.append(f"duplicate topic-for project: {project}")
            continue
        seen_overrides.add(project)
        normalized_overrides.append((project, query))

    normalized_selections: dict[str, str] = {}
    for index, expression in enumerate(selections):
        try:
            project, article = _split_expression(expression, f"select[{index}]")
        except ResolveInputError as error:
            errors.append(str(error))
            continue
        if project not in seen_projects:
            errors.append(f"select project is not requested: {project}")
            continue
        if project not in allowed:
            errors.append(f"select project is not in the committed allowlist: {project}")
            continue
        if project in normalized_selections:
            errors.append(f"duplicate selection for project: {project}")
            continue
        normalized_selections[project] = article

    if errors:
        raise ResolveInputError("\n".join(f"- {error}" for error in errors))
    return normalized_projects, normalized_overrides, normalized_selections


def action_api_url(project: str, params: Mapping[str, str]) -> str:
    """Construct an Action endpoint only after exact local allowlist membership."""
    if project not in load_allowed_projects():
        raise ResolveInputError(f"project is not in the committed allowlist: {project}")
    return f"https://{project}/w/api.php?{urlencode(params)}"


def search_url(project: str, query: str, limit: int = MAX_SEARCH_RESULTS) -> str:
    """Build a namespace-zero relevance search; deliberately omit disabled srwhat."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ResolveInputError("search limit must be an integer from 1 through 50")
    _required_text(query, "search query")
    return action_api_url(
        project,
        {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "errorformat": "plaintext",
            "list": "search",
            "srsearch": query,
            "srnamespace": "0",
            "srlimit": str(limit),
            "srsort": "relevance",
        },
    )


def metadata_url(project: str, titles: Sequence[str]) -> str:
    """Build one batched redirect/namespace/disambiguation metadata request."""
    if not titles or any(not isinstance(title, str) or not title for title in titles):
        raise ResolveInputError("metadata titles must be non-empty strings")
    return action_api_url(
        project,
        {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "errorformat": "plaintext",
            "redirects": "1",
            "titles": "|".join(titles),
            "prop": "info|pageprops",
            "inprop": "url",
            "ppprop": "disambiguation",
        },
    )


def parse_action_payload(body: bytes, expected: str) -> dict[str, object]:
    """Decode strict UTF-8 and reject HTTP-200 Action error envelopes."""
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ResolveResponseError("Action API response is not valid UTF-8 JSON") from error
    if not isinstance(decoded, dict):
        raise ResolveResponseError("Action API response is not a JSON object")
    payload = cast(dict[str, object], decoded)
    if payload.get("error") is not None:
        raise ResolveApiError("action_api_error")
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        raise ResolveApiError("action_api_error")
    if expected not in payload:
        raise ResolveResponseError(f"Action API response is missing {expected!r}")
    return payload


def canonical_article(title: str) -> str:
    """Return the exact UTF-8 AQS component without Unicode normalization."""
    if not isinstance(title, str) or not title:
        raise ResolveResponseError("canonical title must be a non-empty string")
    return quote(title.replace(" ", "_"), safe="")


def _request_json(
    url: str,
    headers: dict[str, str],
    transport: Transport,
    pace: list[float],
) -> dict[str, object]:
    common.throttle(1.0, pace)
    response = transport(url, headers, 30.0)
    if response.status != 200:
        raise ResolveResponseError(f"upstream returned HTTP {response.status}")
    return parse_action_payload(response.body, "query")


def _search_hits(payload: dict[str, object]) -> list[dict[str, object]]:
    query = payload.get("query")
    if not isinstance(query, dict):
        raise ResolveResponseError("Action search response is missing query object")
    hits = cast(dict[str, object], query).get("search")
    if not isinstance(hits, list):
        raise ResolveResponseError("Action search response is missing query.search list")
    if len(hits) > MAX_SEARCH_RESULTS:
        raise ResolveResponseError("Action search response exceeded the candidate limit")
    normalized: list[dict[str, object]] = []
    for index, raw_hit in enumerate(hits):
        if not isinstance(raw_hit, dict):
            raise ResolveResponseError(f"search hit {index} must be an object")
        hit_object = cast(dict[str, object], raw_hit)
        namespace = hit_object.get("ns")
        if isinstance(namespace, bool) or namespace != 0:
            raise ResolveResponseError(f"search hit {index} is not in namespace 0")
        normalized.append(
            {
                "title": _clean_text(hit_object.get("title"), f"search hit {index} title"),
                "rank": index + 1,
            }
        )
    return normalized


def _mapping_from_list(payload: dict[str, object], key: str) -> dict[str, str]:
    query = payload.get("query")
    if not isinstance(query, dict):
        raise ResolveResponseError("Action metadata response is missing query object")
    raw_items = cast(dict[str, object], query).get(key, [])
    if not isinstance(raw_items, list):
        raise ResolveResponseError(f"Action metadata {key} must be a list")
    mapping: dict[str, str] = {}
    for index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, dict):
            raise ResolveResponseError(f"Action metadata {key}[{index}] must be an object")
        item = cast(dict[str, object], raw_item)
        source = _clean_text(item.get("from"), f"{key}[{index}].from")
        target = _clean_text(item.get("to"), f"{key}[{index}].to")
        mapping[source] = target
    return mapping


def _follow_title(
    start: str,
    normalized: Mapping[str, str],
    redirects: Mapping[str, str],
) -> tuple[str, list[str], str | None]:
    title = normalized.get(start, start)
    chain: list[str] = []
    seen = {title}
    for _hop in range(MAX_REDIRECT_HOPS + 1):
        target = redirects.get(title)
        if target is None:
            return title, chain, None
        if target in seen:
            return title, chain, "redirect_cycle"
        chain.append(target)
        seen.add(target)
        title = target
        if len(chain) > MAX_REDIRECT_HOPS:
            return title, chain, "redirect_too_deep"
    return title, chain, "redirect_too_deep"


def _metadata_candidates(
    payload: dict[str, object],
    *,
    effective_query: str,
    search_hits: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    query = payload.get("query")
    if not isinstance(query, dict):
        raise ResolveResponseError("Action metadata response is missing query object")
    pages = cast(dict[str, object], query).get("pages")
    if not isinstance(pages, list):
        raise ResolveResponseError("Action metadata response is missing query.pages list")
    page_by_title: dict[str, dict[str, object]] = {}
    for index, raw_page in enumerate(pages):
        if not isinstance(raw_page, dict):
            raise ResolveResponseError(f"metadata page {index} must be an object")
        page_object = cast(dict[str, object], raw_page)
        title = _clean_text(page_object.get("title"), f"metadata page {index} title")
        page_by_title[title] = page_object

    normalized = _mapping_from_list(payload, "normalized")
    redirects = _mapping_from_list(payload, "redirects")
    exact_normalized_query = normalized.get(effective_query, effective_query)
    grouped: dict[str, dict[str, object]] = {}
    for raw_hit in search_hits:
        raw_title = cast(str, raw_hit["title"])
        rank = cast(int, raw_hit["rank"])
        final_title, redirect_chain, redirect_error = _follow_title(
            raw_title, normalized, redirects
        )
        page = page_by_title.get(final_title)
        if redirect_error is not None or page is None:
            raise ResolveResponseError(
                redirect_error or f"metadata target is missing for search hit {rank}"
            )
        namespace = page.get("ns")
        if isinstance(namespace, bool) or namespace != 0:
            raise ResolveResponseError(f"metadata target for search hit {rank} is not namespace 0")
        page_props = page.get("pageprops")
        if page_props is not None and not isinstance(page_props, dict):
            raise ResolveResponseError("metadata pageprops must be an object")
        if isinstance(page_props, dict) and "disambiguation" in page_props:
            raise ResolveResponseError("metadata target is a disambiguation page")
        article = canonical_article(final_title)
        existing = grouped.get(article)
        if existing is None:
            grouped[article] = {
                "status": "selectable",
                "article": article,
                "title": final_title,
                "namespace": 0,
                "input_titles": [raw_title],
                "redirect_chain": redirect_chain,
                "search_rank": rank,
                "exact_title_match": exact_normalized_query == final_title
                and not redirect_chain,
                "disambiguation": False,
                "reason": None,
            }
        else:
            input_titles = cast(list[str], existing["input_titles"])
            if raw_title not in input_titles:
                input_titles.append(raw_title)
            existing["search_rank"] = min(cast(int, existing["search_rank"]), rank)
    return sorted(
        grouped.values(),
        key=lambda candidate: (
            cast(int, candidate["search_rank"]),
            not cast(bool, candidate["exact_title_match"]),
            cast(str, candidate["article"]),
        ),
    )


def _parse_volume(
    body: bytes,
    *,
    project: str,
    article: str,
    start: date,
    end: date,
) -> dict[str, object]:
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ResolveVolumeError("AQS response is not valid UTF-8 JSON") from error
    if not isinstance(decoded, dict) or not isinstance(decoded.get("items"), list):
        raise ResolveVolumeError("AQS response must contain an items list")
    items = cast(list[object], decoded["items"])
    total = 0
    observed: set[date] = set()
    expected_identity = {
        "project": project,
        "article": article,
        "access": "all-access",
        "agent": "user",
        "granularity": "daily",
    }
    for index, raw_item in enumerate(items):
        if not isinstance(raw_item, dict):
            raise ResolveVolumeError(f"AQS item {index} must be an object")
        item = cast(dict[str, object], raw_item)
        for field, expected in expected_identity.items():
            if field not in item or item[field] != expected:
                raise ResolveVolumeError(f"AQS item {index} has an invalid {field}")
        timestamp = item.get("timestamp")
        views = item.get("views")
        if not isinstance(timestamp, str) or len(timestamp) != 10 or not timestamp.isdigit():
            raise ResolveVolumeError(f"AQS item {index} has an invalid timestamp")
        if timestamp[-2:] != "00":
            raise ResolveVolumeError(f"AQS item {index} has an invalid timestamp")
        if isinstance(views, bool) or not isinstance(views, int) or views < 0:
            raise ResolveVolumeError(f"AQS item {index} has invalid views")
        try:
            observed_date = datetime.strptime(timestamp[:8], "%Y%m%d").date()
        except ValueError as error:
            raise ResolveVolumeError(f"AQS item {index} has an invalid timestamp") from error
        if not start <= observed_date <= end:
            raise ResolveVolumeError(f"AQS item {index} is outside the requested window")
        total += views
        observed.add(observed_date)
    return {
        "status": "available",
        "total_views": total,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "observed_days": len(observed),
        "last_observed_date": max(observed).isoformat() if observed else None,
        "low_volume": total < 1000,
        "reason": None,
    }


def _empty_project(
    project: str,
    effective_query: str,
    query_source: str,
    *,
    search_hits: list[dict[str, object]],
    status: str,
    reason: str,
) -> dict[str, object]:
    return {
        "project": project,
        "effective_query": effective_query,
        "query_source": query_source,
        "status": status,
        "recommendation": None,
        "search_hits": search_hits,
        "candidates": [],
        "selection": None,
        "reason": reason,
        "error": None,
    }


def discover(
    *,
    topic: str,
    projects: Sequence[str],
    overrides: Sequence[tuple[str, str]],
    out: Path,
    today_utc: date,
    transport: Transport,
    pace: list[float],
) -> tuple[dict[str, object] | None, int]:
    """Perform serial discovery and return a complete document without writing it."""
    del out
    ua = common.user_agent()
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in ua):
        raise ResolveInputError("WTI_USER_AGENT must not contain control characters")
    if len(ua) > MAX_DISPLAY_CODE_POINTS:
        raise ResolveInputError("WTI_USER_AGENT is too long")
    headers = {"User-Agent": ua, "Accept": "application/json"}
    override_map = dict(overrides)
    end = today_utc - timedelta(days=1)
    start = end - timedelta(days=VOLUME_DAYS - 1)
    project_documents: list[dict[str, object]] = []

    for project in projects:
        effective_query = override_map.get(project, topic)
        query_source = "project_override" if project in override_map else "shared"
        search_payload = _request_json(search_url(project, effective_query), headers, transport, pace)
        search_hits = _search_hits(search_payload)
        if not search_hits:
            project_documents.append(
                _empty_project(
                    project,
                    effective_query,
                    query_source,
                    search_hits=[],
                    status="unresolved",
                    reason="search_no_hits",
                )
            )
            continue
        metadata_payload = _request_json(
            metadata_url(project, [effective_query, *[cast(str, hit["title"]) for hit in search_hits]]),
            headers,
            transport,
            pace,
        )
        candidates = _metadata_candidates(
            metadata_payload,
            effective_query=effective_query,
            search_hits=search_hits,
        )
        for candidate in candidates:
            article = cast(str, candidate["article"])
            url = fetch_pageviews.series_url(
                project, article, start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
            )
            common.throttle(1.0, pace)
            response = transport(url, headers, 30.0)
            if response.status != 200:
                raise ResolveVolumeError(f"AQS returned HTTP {response.status}")
            candidate["volume"] = _parse_volume(
                response.body,
                project=project,
                article=article,
                start=start,
                end=end,
            )
        selectable = [candidate for candidate in candidates if candidate["status"] == "selectable"]
        if not selectable:
            raise ResolveResponseError("no selectable canonical candidates")
        status = "ready" if len(selectable) == 1 else "ambiguous"
        recommendation = cast(str, selectable[0]["article"]) if len(selectable) == 1 else None
        project_documents.append(
            {
                "project": project,
                "effective_query": effective_query,
                "query_source": query_source,
                "status": status,
                "recommendation": recommendation,
                "search_hits": search_hits,
                "candidates": candidates,
                "selection": None,
                "reason": None if status != "unresolved" else "no_selectable_candidates",
                "error": None,
            }
        )

    unresolved = any(project["status"] == "unresolved" for project in project_documents)
    document: dict[str, object] = {
        "contract_version": CONTRACT_VERSION,
        "run_mode": "discover",
        "status": "unresolved" if unresolved else "awaiting_confirmation",
        "topic": topic,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "volume_window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": VOLUME_DAYS,
            "access": "all-access",
            "agent": "user",
            "granularity": "daily",
        },
        "projects": project_documents,
    }
    return document, 2 if unresolved else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic", required=True, help="shared natural-language topic")
    parser.add_argument(
        "--projects",
        action="append",
        required=True,
        help="allowlisted Wikipedia project code; repeat for ordered project context",
    )
    parser.add_argument("--topic-for", action="append", default=[], help="PROJECT=QUERY override")
    parser.add_argument("--out", default="out/resolved.json", help="resolved manifest path")
    parser.add_argument("--select", action="append", default=[], help="PROJECT=ARTICLE selection")
    parser.add_argument("--reason", help="optional confirmation reason")
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: Transport | None = None,
    pace: list[float] | None = None,
    today_utc: date | None = None,
) -> int:
    """Run offline preflight and the explicit discovery state transition."""
    args = _parser().parse_args(argv)
    common.setup_logging(args.verbose)
    try:
        projects, overrides, selections = preflight_inputs(
            args.topic,
            args.projects,
            args.topic_for,
            args.select,
        )
    except ResolveInputError as error:
        print("resolver input validation failed:", file=sys.stderr)
        print(error, file=sys.stderr)
        return 2

    if selections:
        print("confirmation is not available in this build", file=sys.stderr)
        return 2

    try:
        document, exit_code = discover(
            topic=args.topic.strip(),
            projects=projects,
            overrides=overrides,
            out=Path(args.out),
            today_utc=today_utc or datetime.now(timezone.utc).date(),
            transport=transport or bounded_transport,
            pace=pace if pace is not None else [0.0],
        )
    except SystemExit:
        raise
    except (ResolveInputError, ResolveResponseError, common.TransportError, OSError) as error:
        print(f"resolver failed: {error}", file=sys.stderr)
        return 1

    if document is not None:
        common.dump_json(document, args.out)
        print(f"Resolver {document['status']}: {args.out}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
