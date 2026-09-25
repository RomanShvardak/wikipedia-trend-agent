"""Resolve a natural-language topic to explicit Wikipedia article candidates.

Discovery performs serial Action API search, canonical title metadata, and a
30-complete-day AQS evidence check. Search output is never an implicit
selection: callers inspect the manifest and use the separate confirmation state
transition before downstream spec authoring.
"""
from __future__ import annotations

import argparse
import copy
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


def parse_assignment(value: object, flag_name: str) -> tuple[str, str]:
    """Parse one PROJECT=VALUE assignment without accepting URL-shaped values."""
    if not isinstance(value, str):
        raise ResolveInputError(f"{flag_name} must be a string")
    if "://" in value:
        raise ResolveInputError(f"{flag_name} must not contain a URL")
    project, separator, expression = value.partition("=")
    project = project.strip()
    expression = expression.strip()
    if not separator or not project or not expression:
        raise ResolveInputError(f"{flag_name} must use PROJECT=VALUE")
    if any(
        ord(character) < 0x20 or ord(character) == 0x7F
        for character in project + expression
    ):
        raise ResolveInputError(f"{flag_name} must not contain control characters")
    return project, expression


def _split_expression(value: object, field: str) -> tuple[str, str]:
    return parse_assignment(value, field)


def preflight_inputs(
    topic: str,
    projects: Sequence[str],
    topic_overrides: Sequence[str],
    selections: Sequence[str],
    *,
    max_projects: int = MAX_PROJECTS,
) -> tuple[list[str], list[tuple[str, str]], dict[str, str]]:
    """Aggregate all input violations before any network or artifact side effect."""
    errors: list[str] = []
    if not isinstance(topic, str) or not topic.strip():
        errors.append("topic must be a non-empty string")

    try:
        allowed = load_allowed_projects()
    except ResolveInputError as error:
        allowed = frozenset()
        errors.append(str(error))

    if isinstance(max_projects, bool) or not isinstance(max_projects, int) or max_projects < 1:
        errors.append("max_projects must be a positive integer")
        max_projects = 0

    if isinstance(projects, Sequence) and not isinstance(projects, (str, bytes)):
        project_values = list(projects)
    else:
        project_values = []
        errors.append("projects must be a sequence of project codes")
    if not project_values:
        errors.append("at least one project is required")
    # Check the raw requested entries before allowlist membership or host work.
    if len(project_values) > max_projects:
        errors.append(
            f"at most {max_projects} requested projects are allowed (got {len(project_values)})"
        )

    normalized_projects: list[str] = []
    seen_projects: set[str] = set()
    for index, project in enumerate(project_values):
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

    def parse_assignments(
        values: Sequence[str], flag_name: str
    ) -> tuple[list[tuple[str, str]], dict[str, str]]:
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            errors.append(f"{flag_name} must be a sequence of assignments")
            return [], {}
        parsed: list[tuple[str, str]] = []
        seen: set[str] = set()
        for index, expression in enumerate(values):
            try:
                project, value = _split_expression(expression, f"{flag_name}[{index}]")
            except ResolveInputError as error:
                errors.append(str(error))
                continue
            if project not in seen_projects:
                errors.append(f"{flag_name} project is not requested: {project}")
                continue
            if project not in allowed:
                errors.append(f"{flag_name} project is not in the committed allowlist: {project}")
                continue
            if project in seen:
                errors.append(f"duplicate {flag_name} project: {project}")
                continue
            seen.add(project)
            parsed.append((project, value))
        return parsed, {project: value for project, value in parsed}

    normalized_overrides, _ = parse_assignments(topic_overrides, "topic-for")
    _, normalized_selections = parse_assignments(selections, "select")

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


def follow_redirects(
    start: str,
    normalized: Mapping[str, str],
    redirects: Mapping[str, str],
    max_hops: int = MAX_REDIRECT_HOPS,
) -> tuple[str, list[str], str | None]:
    """Reconstruct one normalized redirect chain with cycle and depth guards."""
    if isinstance(max_hops, bool) or not isinstance(max_hops, int) or max_hops < 0:
        raise ResolveInputError("max_hops must be a non-negative integer")
    title = normalized.get(start, start)
    chain: list[str] = []
    seen = {title}
    while True:
        target = redirects.get(title)
        if target is None:
            return title, chain, None
        chain.append(target)
        if target in seen:
            return target, chain, "redirect_cycle"
        if len(chain) > max_hops:
            return target, chain, "redirect_too_deep"
        seen.add(target)
        title = target


def _unresolved_candidate(
    *,
    input_title: str,
    search_rank: int,
    final_title: str,
    redirect_chain: list[str],
    reason: str,
    namespace: int | None = None,
    disambiguation: bool = False,
    exact_title_match: bool = False,
) -> dict[str, object]:
    return {
        "status": "unresolved",
        "article": None,
        "title": final_title,
        "namespace": namespace,
        "input_titles": [input_title],
        "redirect_chain": redirect_chain,
        "search_rank": search_rank,
        "exact_title_match": exact_title_match,
        "disambiguation": disambiguation,
        "reason": reason,
    }


def parse_metadata(
    body: bytes | Mapping[str, object],
    search_titles: Sequence[Mapping[str, object]],
    effective_query: str,
) -> list[dict[str, object]]:
    """Classify every search hit against its final redirect and page target."""
    payload = (
        parse_action_payload(body, "query")
        if isinstance(body, bytes)
        else dict(body)
    )
    if "query" not in payload:
        raise ResolveResponseError("Action metadata response is missing 'query'")
    query = cast(dict[str, object], payload["query"])
    pages = query.get("pages")
    if not isinstance(pages, list):
        raise ResolveResponseError("Action metadata response is missing query.pages list")

    page_by_title: dict[str, dict[str, object]] = {}
    for index, raw_page in enumerate(pages):
        if not isinstance(raw_page, dict):
            raise ResolveResponseError(f"metadata page {index} must be an object")
        page_object = cast(dict[str, object], raw_page)
        if page_object.get("missing") is True:
            continue
        title = _clean_text(page_object.get("title"), f"metadata page {index} title")
        if title in page_by_title:
            raise ResolveResponseError(f"metadata response repeats page title: {title}")
        page_by_title[title] = page_object

    normalized = _mapping_from_list(payload, "normalized")
    redirects = _mapping_from_list(payload, "redirects")
    exact_normalized_query = normalized.get(effective_query, effective_query)
    grouped: dict[str, dict[str, object]] = {}
    candidates: list[dict[str, object]] = []

    for hit_index, raw_hit in enumerate(search_titles):
        raw_title = _clean_text(raw_hit.get("title"), f"search title {hit_index}")
        raw_rank = raw_hit.get("rank")
        if isinstance(raw_rank, bool) or not isinstance(raw_rank, int) or raw_rank < 1:
            raise ResolveResponseError(f"search title {hit_index} has an invalid rank")
        final_title, redirect_chain, redirect_error = follow_redirects(
            raw_title, normalized, redirects
        )
        page = page_by_title.get(final_title)
        if redirect_error is not None:
            candidates.append(
                _unresolved_candidate(
                    input_title=raw_title,
                    search_rank=raw_rank,
                    final_title=final_title,
                    redirect_chain=redirect_chain,
                    reason=redirect_error,
                    exact_title_match=exact_normalized_query == final_title
                    and not redirect_chain,
                )
            )
            continue
        if page is None:
            candidates.append(
                _unresolved_candidate(
                    input_title=raw_title,
                    search_rank=raw_rank,
                    final_title=final_title,
                    redirect_chain=redirect_chain,
                    reason="missing_target",
                )
            )
            continue

        namespace_value = page.get("ns")
        if isinstance(namespace_value, bool) or namespace_value != 0:
            candidates.append(
                _unresolved_candidate(
                    input_title=raw_title,
                    search_rank=raw_rank,
                    final_title=final_title,
                    redirect_chain=redirect_chain,
                    reason="non_article_namespace",
                    namespace=(
                        namespace_value
                        if isinstance(namespace_value, int)
                        and not isinstance(namespace_value, bool)
                        else None
                    ),
                )
            )
            continue

        page_props = page.get("pageprops")
        if page_props is not None and not isinstance(page_props, dict):
            raise ResolveResponseError("metadata pageprops must be an object")
        is_disambiguation = (
            isinstance(page_props, dict) and "disambiguation" in page_props
        )
        if is_disambiguation:
            candidates.append(
                _unresolved_candidate(
                    input_title=raw_title,
                    search_rank=raw_rank,
                    final_title=final_title,
                    redirect_chain=redirect_chain,
                    reason="disambiguation_page",
                    namespace=0,
                    disambiguation=True,
                )
            )
            continue

        article = canonical_article(final_title)
        exact_title_match = (
            exact_normalized_query == final_title and not redirect_chain
        )
        existing = grouped.get(article)
        if existing is None:
            grouped[article] = {
                "status": "selectable",
                "article": article,
                "title": final_title,
                "namespace": 0,
                "input_titles": [raw_title],
                "redirect_chain": redirect_chain,
                "search_rank": raw_rank,
                "exact_title_match": exact_title_match,
                "disambiguation": False,
                "reason": None,
            }
        else:
            input_titles = cast(list[str], existing["input_titles"])
            if raw_title not in input_titles:
                input_titles.append(raw_title)
            existing["search_rank"] = min(cast(int, existing["search_rank"]), raw_rank)
            existing["exact_title_match"] = (
                cast(bool, existing["exact_title_match"]) or exact_title_match
            )

    candidates.extend(grouped.values())
    return sorted(
        candidates,
        key=lambda candidate: (
            cast(int, candidate["search_rank"]),
            not cast(bool, candidate["exact_title_match"]),
            cast(str, candidate["article"] or ""),
        ),
    )


def recommend_candidates(candidates: Sequence[Mapping[str, object]]) -> str | None:
    """Recommend only one selectable target or one direct exact-title target."""
    selectable = [
        candidate
        for candidate in candidates
        if candidate.get("status") == "selectable" and isinstance(candidate.get("article"), str)
    ]
    if len(selectable) == 1:
        return cast(str, selectable[0]["article"])
    exact = [
        candidate for candidate in selectable if candidate.get("exact_title_match") is True
    ]
    if len(exact) == 1:
        return cast(str, exact[0]["article"])
    return None


def volume_window(today_utc: date) -> tuple[date, date]:
    """Return one shared inclusive window of 30 complete UTC calendar days."""
    if not isinstance(today_utc, date):
        raise ResolveInputError("today_utc must be a date")
    end = today_utc - timedelta(days=1)
    return end - timedelta(days=VOLUME_DAYS - 1), end


def _validate_volume_window(start: date, end: date) -> None:
    if (end - start).days + 1 != VOLUME_DAYS:
        raise ResolveVolumeError(
            "AQS validation window must contain exactly 30 days"
        )


def validate_volume_payload(
    payload: dict[str, object],
    *,
    expected_project: str,
    expected_article: str,
    expected_access: str = "all-access",
    expected_agent: str = "user",
    expected_granularity: str = "daily",
    start: date,
    end: date,
) -> None:
    """Validate all AQS items before any total is accumulated or exposed."""
    _validate_volume_window(start, end)
    items = payload.get("items")
    if not isinstance(items, list):
        raise ResolveVolumeError("AQS response must contain an items list")
    if len(items) > VOLUME_DAYS:
        raise ResolveVolumeError("AQS response exceeded the 30-item window limit")

    expected_identity = {
        "project": expected_project,
        "article": expected_article,
        "access": expected_access,
        "agent": expected_agent,
        "granularity": expected_granularity,
    }
    observed: set[date] = set()
    for index, raw_item in enumerate(items):
        if not isinstance(raw_item, dict):
            raise ResolveVolumeError(f"AQS item {index} must be an object")
        item = cast(dict[str, object], raw_item)
        for field, expected in expected_identity.items():
            actual = item.get(field)
            if not isinstance(actual, str) or actual != expected:
                raise ResolveVolumeError(f"AQS item {index} has an invalid {field}")

        timestamp = item.get("timestamp")
        if (
            not isinstance(timestamp, str)
            or len(timestamp) != 10
            or not timestamp.isdigit()
            or timestamp[-2:] != "00"
        ):
            raise ResolveVolumeError(f"AQS item {index} has an invalid timestamp")
        try:
            observed_date = datetime.strptime(timestamp[:8], "%Y%m%d").date()
        except ValueError as error:
            raise ResolveVolumeError(f"AQS item {index} has an invalid timestamp") from error
        if observed_date in observed:
            raise ResolveVolumeError(f"AQS item {index} has a duplicate date")
        if not start <= observed_date <= end:
            raise ResolveVolumeError(f"AQS item {index} is outside the requested window")
        views = item.get("views")
        if isinstance(views, bool) or not isinstance(views, int) or views < 0:
            raise ResolveVolumeError(f"AQS item {index} has invalid views")
        observed.add(observed_date)


def parse_volume_response(
    response: TransportResponse,
    *,
    expected_project: str,
    expected_article: str,
    expected_access: str = "all-access",
    expected_agent: str = "user",
    expected_granularity: str = "daily",
    start: date,
    end: date,
) -> dict[str, object]:
    """Return exact available or explicitly unavailable AQS evidence."""
    _validate_volume_window(start, end)
    if response.status == 404:
        return {
            "status": "unavailable",
            "total_views": None,
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "observed_days": None,
            "last_observed_date": None,
            "low_volume": None,
            "reason": "aqs_404_zero_or_not_loaded",
        }
    if response.status != 200:
        raise ResolveVolumeError(f"AQS returned HTTP {response.status}")

    try:
        decoded = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ResolveVolumeError("AQS response is not valid UTF-8 JSON") from error
    if not isinstance(decoded, dict):
        raise ResolveVolumeError("AQS response must be a JSON object")
    payload = cast(dict[str, object], decoded)
    validate_volume_payload(
        payload,
        expected_project=expected_project,
        expected_article=expected_article,
        expected_access=expected_access,
        expected_agent=expected_agent,
        expected_granularity=expected_granularity,
        start=start,
        end=end,
    )

    items = cast(list[object], payload["items"])
    total = 0
    observed: set[date] = set()
    for raw_item in items:
        item = cast(dict[str, object], raw_item)
        views = cast(int, item["views"])
        observed_date = datetime.strptime(
            cast(str, item["timestamp"]), "%Y%m%d%H"
        ).date()
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


def _error_record(error: BaseException) -> dict[str, str]:
    """Return a stable, bounded diagnostic without retaining an upstream body."""
    if isinstance(error, ResolveApiError):
        code = "action_api_error"
    elif isinstance(error, ResolveVolumeError):
        code = "aqs_error"
    elif isinstance(error, common.TransportError):
        code = "transport_error"
    elif isinstance(error, ResolveResponseError):
        code = "response_error"
    else:
        code = "io_error"
    message = "".join(
        character
        for character in str(error)
        if not (0 <= ord(character) <= 0x1F or 0x7F <= ord(character) <= 0x9F)
    )[:MAX_DISPLAY_CODE_POINTS]
    return {"code": code, "message": message or code}


def aggregate_project_outcomes(
    project_records: Sequence[Mapping[str, object]],
) -> tuple[str, int]:
    """Derive the top outcome and exit code while preserving project records."""
    statuses = [record.get("status") for record in project_records]
    if statuses and all(status == "error" for status in statuses):
        return "error", 1
    if "unresolved" in statuses:
        return "unresolved", 2
    if "error" in statuses and any(
        status in {"ready", "ambiguous"} for status in statuses
    ):
        return "partial_error", 3
    return "awaiting_confirmation", 0


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


def _failed_project(
    project: str,
    effective_query: str,
    query_source: str,
    *,
    search_hits: list[dict[str, object]] | None,
    candidates: list[dict[str, object]],
    error: BaseException,
) -> dict[str, object]:
    return {
        "project": project,
        "effective_query": effective_query,
        "query_source": query_source,
        "status": "error",
        "recommendation": None,
        "search_hits": search_hits,
        "candidates": candidates,
        "selection": None,
        "reason": None,
        "error": _error_record(error),
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
    """Perform serial discovery and return one complete document without writing it."""
    del out
    ua = common.user_agent()
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in ua):
        raise ResolveInputError("WTI_USER_AGENT must not contain control characters")
    if len(ua) > MAX_DISPLAY_CODE_POINTS:
        raise ResolveInputError("WTI_USER_AGENT is too long")
    headers = {"User-Agent": ua, "Accept": "application/json"}
    override_map = dict(overrides)
    start, end = volume_window(today_utc)
    project_documents: list[dict[str, object]] = []

    for project in projects:
        effective_query = override_map.get(project, topic)
        query_source = "project_override" if project in override_map else "shared"
        search_hits: list[dict[str, object]] | None = None
        candidates: list[dict[str, object]] = []
        try:
            search_payload = _request_json(
                search_url(project, effective_query), headers, transport, pace
            )
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
            metadata_titles = [
                effective_query,
                *[cast(str, hit["title"]) for hit in search_hits],
            ]
            metadata_payload = _request_json(
                metadata_url(project, metadata_titles),
                headers,
                transport,
                pace,
            )
            candidates = parse_metadata(metadata_payload, search_hits, effective_query)
            for candidate in candidates:
                if candidate["status"] != "selectable":
                    continue
                article = cast(str, candidate["article"])
                url = fetch_pageviews.series_url(
                    project, article, start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
                )
                common.throttle(1.0, pace)
                response = transport(url, headers, 30.0)
                candidate["volume"] = parse_volume_response(
                    response,
                    expected_project=project,
                    expected_article=article,
                    start=start,
                    end=end,
                )
        except (ResolveResponseError, common.TransportError, OSError) as error:
            project_documents.append(
                _failed_project(
                    project,
                    effective_query,
                    query_source,
                    search_hits=search_hits,
                    candidates=candidates,
                    error=error,
                )
            )
            continue

        selectable = [candidate for candidate in candidates if candidate["status"] == "selectable"]
        status = "ready" if len(selectable) == 1 else "ambiguous" if selectable else "unresolved"
        project_documents.append(
            {
                "project": project,
                "effective_query": effective_query,
                "query_source": query_source,
                "status": status,
                "recommendation": recommend_candidates(selectable),
                "search_hits": search_hits,
                "candidates": candidates,
                "selection": None,
                "reason": None if status != "unresolved" else "no_selectable_candidates",
                "error": None,
            }
        )

    top_status, exit_code = aggregate_project_outcomes(project_documents)
    document: dict[str, object] = {
        "contract_version": CONTRACT_VERSION,
        "run_mode": "discover",
        "status": top_status,
        "topic": topic,
        "topic_overrides": [
            {"project": project, "query": query} for project, query in overrides
        ],
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
    return document, exit_code


def _confirmation_error(messages: Sequence[str]) -> tuple[None, int]:
    print("resolver confirmation failed:", file=sys.stderr)
    for message in messages:
        print(f"- {message}", file=sys.stderr)
    return None, 2


def confirm(
    *,
    topic: str,
    projects: Sequence[str],
    topic_overrides: Sequence[tuple[str, str]],
    selections: Mapping[str, str],
    reason: str | None,
    out: Path,
) -> tuple[dict[str, object] | None, int]:
    """Validate and transform the saved discovery manifest without network access."""
    errors: list[str] = []
    normalized_topic = topic.strip() if isinstance(topic, str) else ""
    if not normalized_topic:
        errors.append("topic must be a non-empty string")
    requested_projects = list(projects)
    if not requested_projects or any(
        not isinstance(project, str) for project in requested_projects
    ):
        errors.append("confirmation requires the ordered project context")
    if len(set(requested_projects)) != len(requested_projects):
        errors.append("confirmation project context contains duplicates")

    override_map: dict[str, str] = {}
    for override_project, query in topic_overrides:
        if override_project in override_map:
            errors.append(
                f"confirmation override context is duplicated for {override_project}"
            )
        if not isinstance(query, str) or not query:
            errors.append(
                f"confirmation override query is invalid for {override_project}"
            )
        override_map[override_project] = query
    if set(override_map) - set(requested_projects):
        errors.append("confirmation override context contains an unrequested project")

    try:
        decoded = json.loads(out.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _confirmation_error(["discovery manifest does not exist"])
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _confirmation_error(["discovery manifest is not valid UTF-8 JSON"])
    if not isinstance(decoded, dict):
        return _confirmation_error(["discovery manifest must be a JSON object"])
    document = cast(dict[str, object], decoded)
    if document.get("contract_version") != CONTRACT_VERSION:
        errors.append("discovery manifest has the wrong contract_version")
    if document.get("run_mode") != "discover":
        errors.append("discovery manifest must have run_mode='discover'")
    if document.get("status") != "awaiting_confirmation":
        errors.append("discovery manifest must have status='awaiting_confirmation'")
    if document.get("topic") != normalized_topic:
        errors.append("topic does not match the saved discovery context")
    expected_overrides = [
        {"project": project, "query": query} for project, query in topic_overrides
    ]
    if document.get("topic_overrides") != expected_overrides:
        errors.append("ordered topic-for context does not match the saved discovery manifest")

    saved_projects_value = document.get("projects")
    if not isinstance(saved_projects_value, list):
        return _confirmation_error([*errors, "discovery manifest projects must be a list"])
    saved_projects: list[dict[str, object]] = []
    for index, raw_project in enumerate(saved_projects_value):
        if not isinstance(raw_project, dict):
            errors.append(f"saved project {index} must be an object")
            continue
        saved_projects.append(cast(dict[str, object], raw_project))
    saved_codes = [project.get("project") for project in saved_projects]
    if saved_codes != requested_projects:
        errors.append("ordered project context does not match the saved discovery manifest")

    selection_projects = set(selections)
    requested_set = set(requested_projects)
    if selection_projects != requested_set:
        missing = sorted(requested_set - selection_projects)
        extra = sorted(selection_projects - requested_set)
        if missing:
            errors.append(f"missing selection for project: {', '.join(missing)}")
        if extra:
            errors.append(f"selection contains unrequested project: {', '.join(extra)}")

    normalized_reason = reason.strip() if isinstance(reason, str) and reason.strip() else None
    selected_candidates: dict[str, dict[str, object]] = {}
    for saved_project in saved_projects:
        code = saved_project.get("project")
        if not isinstance(code, str) or code not in requested_set:
            continue
        expected_query = override_map.get(code, normalized_topic)
        expected_source = "project_override" if code in override_map else "shared"
        if saved_project.get("effective_query") != expected_query:
            errors.append(f"effective_query does not match for project: {code}")
        if saved_project.get("query_source") != expected_source:
            errors.append(f"query_source does not match for project: {code}")
        if saved_project.get("selection") is not None:
            errors.append(f"saved project already has a selection: {code}")
        status = saved_project.get("status")
        if status not in {"ready", "ambiguous"}:
            errors.append(f"saved project is not confirmable: {code}")
        if status == "ambiguous" and normalized_reason is None:
            errors.append(f"ambiguous project requires a reason: {code}")
        selected_article = selections.get(code)
        if not isinstance(selected_article, str) or not selected_article:
            errors.append(f"selection article must be a non-empty string: {code}")
            continue
        candidates = saved_project.get("candidates")
        if not isinstance(candidates, list):
            errors.append(f"saved candidates must be a list: {code}")
            continue
        matches = [
            candidate
            for candidate in candidates
            if isinstance(candidate, dict)
            and candidate.get("status") == "selectable"
            and candidate.get("article") == selected_article
        ]
        if len(matches) != 1:
            errors.append(f"selection is not one exact saved candidate: {code}={selected_article}")
            continue
        candidate = cast(dict[str, object], matches[0])
        title = candidate.get("title")
        if not isinstance(title, str) or not title:
            errors.append(f"selected candidate has an invalid title: {code}")
            continue
        selected_candidates[code] = candidate

    if errors:
        return _confirmation_error(errors)

    confirmed = copy.deepcopy(document)
    confirmed["run_mode"] = "confirm"
    confirmed["status"] = "confirmed"
    confirmed_projects = cast(list[dict[str, object]], confirmed["projects"])
    for confirmed_project in confirmed_projects:
        code = cast(str, confirmed_project["project"])
        candidate = selected_candidates[code]
        confirmed_project["selection"] = {
            "article": candidate["article"],
            "title": candidate["title"],
            "reason": normalized_reason,
            "source": "model_confirmation",
        }
    return confirmed, 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic", required=True, help="shared natural-language topic")
    parser.add_argument(
        "--projects",
        action="append",
        nargs="+",
        required=True,
        help="one or more allowlisted Wikipedia project codes; repeat for ordered context",
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
        requested_projects = [
            project
            for project_group in args.projects
            for project in project_group
        ]
        projects, overrides, selections = preflight_inputs(
            args.topic,
            requested_projects,
            args.topic_for,
            args.select,
        )
    except ResolveInputError as error:
        print("resolver input validation failed:", file=sys.stderr)
        print(error, file=sys.stderr)
        return 2

    if args.select:
        try:
            document, exit_code = confirm(
                topic=args.topic,
                projects=projects,
                topic_overrides=overrides,
                selections=selections,
                reason=args.reason,
                out=Path(args.out),
            )
        except (ResolveInputError, TypeError, ValueError) as error:
            print(f"resolver confirmation failed: {error}", file=sys.stderr)
            return 2
        if document is not None and exit_code == 0:
            common.dump_json(document, args.out)
            print(f"Resolver confirmed: {args.out}")
        return exit_code

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
