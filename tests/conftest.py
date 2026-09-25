"""Shared pytest fixtures for the wikipedia-trend-agent tests.

The sys.path shim goes FIRST so every phase test can `import common`
without rootdir/path gymnastics (D-06: everything lives in the skill dir).
No network anywhere.
"""
import sys
from pathlib import Path

# Shared import shim: scripts dir on sys.path before anything else.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pytest  # noqa: E402  (after the shim — deliberate)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _forbidden_transport(url: str, headers: dict[str, str], timeout: float = 30.0) -> None:
    """Fail immediately if a zero-network test attempts an HTTP exchange."""
    raise AssertionError("network access is forbidden in resolver tests")


def _resolve_fixture(name: str) -> bytes:
    """Read one committed fixture without allowing path or symlink escape."""
    requested = Path(name)
    fixture_root = FIXTURES_DIR.resolve()
    assert not requested.is_absolute(), "fixture name must be relative"
    assert requested.name == name, "fixture name must not contain directories"
    assert requested.suffix == ".json", "fixture name must end in .json"
    candidate = (fixture_root / requested).resolve()
    assert candidate.parent == fixture_root, "fixture path escapes the fixture directory"
    assert candidate.is_file(), f"fixture does not exist: {name}"
    return candidate.read_bytes()


@pytest.fixture
def forbidden_transport():
    """Return a transport callable that rejects its first invocation."""
    return _forbidden_transport


@pytest.fixture
def resolve_fixture():
    """Return the contained committed-fixture loader."""
    return _resolve_fixture


class StubResponse:
    """TransportResponse-like object served by the transport stub (status/headers/body).

    Attribute shape mirrors fetch_pageviews.TransportResponse so stub-backed
    runs exercise the exact same client code path as the real transport.
    """

    def __init__(self, status: int, headers: dict, body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body


@pytest.fixture
def spec_example_path() -> Path:
    """Path to the committed golden VALID spec fixture (the frozen contract's executable spec)."""
    return FIXTURES_DIR / "spec.example.json"


@pytest.fixture
def live_spec_path() -> Path:
    """Path to the committed live-article spec fixture (probe-verified pl.wikipedia/Warszawa + en.wikipedia/Albert_Einstein)."""
    return FIXTURES_DIR / "spec.live.example.json"


@pytest.fixture
def transport_stub():
    """Factory for a scripted zero-network transport: transport_stub([(status, headers, body), ...]).

    Returns a callable matching the fetch transport signature (url, headers, timeout)
    that pops responses in order, records every call as (url, headers) on .calls —
    letting tests assert UA presence, call counts, and request URLs — and raises
    AssertionError when called more times than scripted (a test bug, not a network hit).
    """

    def factory(responses):
        responses = list(responses)

        def stub(url, headers, timeout=30.0):
            stub.calls.append((url, dict(headers)))
            if not responses:
                raise AssertionError(
                    f"transport stub called {len(stub.calls)} times but only scripted "
                    f"{len(stub.calls) - 1} responses"
                )
            status, resp_headers, body = responses.pop(0)
            return StubResponse(status, resp_headers, body)

        stub.calls = []
        return stub

    return factory