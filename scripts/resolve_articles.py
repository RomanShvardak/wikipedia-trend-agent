"""Resolve a natural-language topic to explicit Wikipedia article candidates."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import common
from common import Transport, TransportResponse

CONTRACT_VERSION = "resolved.v1"
MAX_RESPONSE_BYTES = 1_048_576
PROJECTS_FILE = Path(__file__).resolve().parents[1] / "assets" / "wikipedia-projects.json"


class ResolveInputError(ValueError):
    """Resolver input failed offline validation."""


class ResolveResponseError(RuntimeError):
    """An upstream response did not match the required contract."""


class ResolveApiError(ResolveResponseError):
    """The Action API returned an error envelope inside HTTP 200."""


def load_allowed_projects() -> frozenset[str]:
    """Return exact project codes from the committed offline catalog."""
    return frozenset()


def bounded_transport(
    url: str,
    headers: dict[str, str],
    timeout: float = 30.0,
) -> TransportResponse:
    """Transport adapter used by discovery."""
    return common.TransportResponse(status=200, headers={}, body=b"")


def preflight_inputs(
    topic: str,
    projects: Sequence[str],
    topic_overrides: Sequence[str],
    selections: Sequence[str],
) -> tuple[list[str], list[tuple[str, str]], dict[str, str]]:
    """Validate CLI context before User-Agent or transport work."""
    return [], [], {}


def action_api_url(project: str, params: Mapping[str, str]) -> str:
    """Build an allowlisted Action API URL."""
    return ""


def search_url(project: str, query: str, limit: int = 5) -> str:
    """Build the full-text namespace-zero search URL."""
    return ""


def metadata_url(project: str, titles: Sequence[str]) -> str:
    """Build the redirect/page metadata URL."""
    return ""


def parse_action_payload(body: bytes, expected: str) -> dict[str, object]:
    """Parse a successful Action API object."""
    return {}


def canonical_article(title: str) -> str:
    """Encode a canonical title as an exact UTF-8 AQS article component."""
    return ""


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
    """Discover and return one complete manifest without publishing it."""
    return None, 0


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: Transport | None = None,
    pace: list[float] | None = None,
    today_utc: date | None = None,
) -> int:
    """Run resolver input parsing and state selection."""
    return 0
