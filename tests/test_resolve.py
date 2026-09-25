"""Zero-network bootstrap guards for future article-resolution tests.

This Wave 0 file intentionally does not import resolve_articles.py. It proves
that later resolver tests have an immediately failing transport and a fixture
loader that cannot escape the committed fixture directory.
"""
import json
from pathlib import Path

import pytest

import conftest


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
