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


@pytest.fixture
def spec_example_path() -> Path:
    """Path to the committed golden VALID spec fixture (the frozen contract's executable spec)."""
    return FIXTURES_DIR / "spec.example.json"