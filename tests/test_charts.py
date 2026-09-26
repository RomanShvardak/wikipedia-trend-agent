"""Offline tests for the Wikipedia trend chart stage.

Zero-network guarantee: this module never opens a socket — the chart stage reads
only committed fixtures plus the analyzer's own pure helpers, and every test
asserts the committed fixture bytes are unchanged after it runs.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import analyze_trends


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

COMMITTED_FIXTURES = (
    "series.anomalies.example.csv",
    "metrics.anomalies.example.json",
    "series.gaps.example.csv",
)


def test_committed_anomalies_pair_regenerates_exactly_and_shows_raw_clean_divergence(
    tmp_path: Path, tmp_out: Path
) -> None:
    """The committed spike pair regenerates exactly and diverges raw vs clean on one day."""
    scripts_dir = str(Path(__file__).resolve().parents[1] / "scripts")
    assert scripts_dir in sys.path, "conftest shim must expose scripts/ for stage imports"

    # tmp_out is the chart stage's per-test output directory: under tmp_path, never
    # the repository's out/, and empty before this test puts anything in it.
    assert tmp_out.is_dir(), "tmp_out must be an existing directory"
    assert tmp_out.parent == tmp_path, "tmp_out must live under tmp_path"
    assert tmp_out != FIXTURES_DIR, "tmp_out must not point into the committed fixtures"

    before = {name: FIXTURES_DIR.joinpath(name).read_bytes() for name in COMMITTED_FIXTURES}
    spec = json.loads(FIXTURES_DIR.joinpath("spec.example.json").read_text(encoding="utf-8"))
    committed = json.loads(
        FIXTURES_DIR.joinpath("metrics.anomalies.example.json").read_text(encoding="utf-8")
    )
    golden = json.loads(FIXTURES_DIR.joinpath("metrics.example.json").read_text(encoding="utf-8"))

    # Work on a copy: the committed CSV is an input, never a scratch file.
    working = tmp_out / "series.csv"
    working.write_bytes(before["series.anomalies.example.csv"])

    grouped = analyze_trends.load_series_csv(working, spec)
    regenerated = analyze_trends.build_metrics(
        spec, "tests/fixtures/spec.example.json", grouped
    )

    assert regenerated["series"] == committed["series"]
    assert set(committed) == {"spec_name", "as_of", "generated_from", "series"}
    # Key-set comparison, never a count literal: metrics.v1 may still grow fields.
    assert set(committed["series"][0]) == set(golden["series"][0])

    pl = next(s for s in committed["series"] if s["series_id"] == "pl-post-przerywany")
    assert [a["date"] for a in pl["anomalies"]] == ["2026-03-15"]
    assert pl["anomalies"][0]["value"] == 25380
    assert pl["anomalies"][0]["median"] == 2539
    # D-03 thesis made executable: one 10x day moves raw growth, not clean growth.
    assert pl["growth"]["y1"]["pct"] == 19.6
    assert pl["growth"]["y1"]["clean"]["pct"] == 16.7

    cs_committed = next(
        s for s in committed["series"] if s["series_id"] == "cs-pust-prerusovany"
    )
    cs_golden = next(s for s in golden["series"] if s["series_id"] == "cs-pust-prerusovany")
    assert cs_committed == cs_golden

    for name in COMMITTED_FIXTURES:
        assert FIXTURES_DIR.joinpath(name).read_bytes() == before[name], (
            f"the test run mutated the committed fixture {name}"
        )
