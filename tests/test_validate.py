"""Validator contract tests: aggregated errors, SystemExit(2), fixture-as-executable-spec.

Pins the frozen D-01/D-02 spec contract and the D-07 (aggregated) / D-08
(SystemExit(2) + stderr) error contract. No network access; no matplotlib
import (NumPy/ABI-broken envs — RESEARCH Pitfall 5).
"""
import copy
import json

import pytest

from common import load_and_validate_spec, validate_spec


def _mutated(tmp_path, spec_example_path, mutator) -> str:
    """Write a mutated copy of the golden fixture and return its path."""
    spec = copy.deepcopy(json.loads(spec_example_path.read_text(encoding="utf-8")))
    mutator(spec)
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(p)


def test_valid_spec_loads(spec_example_path):
    """The committed golden spec validates with zero errors (fixture = executable contract)."""
    spec = load_and_validate_spec(spec_example_path)
    for key in ("name", "request", "language", "window", "series"):
        assert key in spec
    assert len(spec["series"]) == 2


def test_missing_fields_aggregated(spec_example_path, tmp_path, capsys):
    """Two independent violations surface in ONE aggregated stderr message (D-07)."""
    path = _mutated(
        tmp_path,
        spec_example_path,
        lambda s: (s.pop("request"), s["series"][0].pop("project")),
    )
    with pytest.raises(SystemExit) as excinfo:
        load_and_validate_spec(path)
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "spec.json validation failed (2 errors):" in err
    assert "root.request: missing required field" in err
    assert "series[0].project: missing required field" in err
    assert "how to fix" in err


def test_unknown_field_rejected(spec_example_path, tmp_path, capsys):
    """Extra root keys are rejected with the unknown-field error line."""
    path = _mutated(tmp_path, spec_example_path, lambda s: s.__setitem__("foo", "bar"))
    with pytest.raises(SystemExit) as excinfo:
        load_and_validate_spec(path)
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert 'unknown field "foo" at root' in err
    assert "how to fix" in err


def test_bad_date_format(spec_example_path, tmp_path, capsys):
    """window.end must be YYYYMMDD; a dashed date is rejected with a fix hint."""
    path = _mutated(tmp_path, spec_example_path, lambda s: s["window"].__setitem__("end", "2026-09-20"))
    with pytest.raises(SystemExit) as excinfo:
        load_and_validate_spec(path)
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "window.end" in err
    assert "expected YYYYMMDD" in err


def test_start_after_end_rejected(spec_example_path, tmp_path, capsys):
    """window.start must precede window.end."""
    path = _mutated(
        tmp_path,
        spec_example_path,
        lambda s: (s["window"].__setitem__("start", "20260920"), s["window"].__setitem__("end", "20240923")),
    )
    with pytest.raises(SystemExit) as excinfo:
        load_and_validate_spec(path)
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "must be before" in err
    assert "20260920" in err


def test_granularity_monthly_rejected(spec_example_path, tmp_path, capsys):
    """monthly is a v2 (HARD-01) granularity — v1 allows only 'daily'."""
    path = _mutated(tmp_path, spec_example_path, lambda s: s["window"].__setitem__("granularity", "monthly"))
    with pytest.raises(SystemExit) as excinfo:
        load_and_validate_spec(path)
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert '"daily"' in err


def test_empty_series_rejected(spec_example_path, tmp_path, capsys):
    """series [] is a contract violation (full series[] required, D-01)."""
    path = _mutated(tmp_path, spec_example_path, lambda s: s.__setitem__("series", []))
    with pytest.raises(SystemExit) as excinfo:
        load_and_validate_spec(path)
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "series" in err
    assert "non-empty" in err


def test_unknown_series_key_rejected(spec_example_path, tmp_path, capsys):
    """Extra keys on a series item are rejected."""
    path = _mutated(tmp_path, spec_example_path, lambda s: s["series"][0].__setitem__("foo", "bar"))
    with pytest.raises(SystemExit) as excinfo:
        load_and_validate_spec(path)
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert 'unknown field "foo" at series[0]' in err


def test_load_missing_file(tmp_path):
    """A nonexistent spec path is a SystemExit with a 'Failed to load spec' message."""
    with pytest.raises(SystemExit) as excinfo:
        load_and_validate_spec(tmp_path / "nope.json")
    assert "Failed to load spec" in str(excinfo.value)


def test_validate_spec_returns_list_for_malformed_input():
    """validate_spec NEVER raises on malformed input — it returns a list (D-07)."""
    errors = validate_spec("not-a-dict")  # type: ignore[arg-type]
    assert isinstance(errors, list)
    assert len(errors) == 1