"""Zero-network end-to-end tests for the four-stage orchestrator (`run_all.py`).

Every test in this module is isolated by three seams, and nothing else: the
transport is injected through `run_all`'s keyword-only `transport` argument,
`common.CACHE_DIR` is repointed at `tmp_path` so the repository's own `.cache/`
is never written, and `common.throttle` is replaced by a no-op so the four
requests cost no wall-clock. No test here reaches the network, and none writes
into the repository's `out/`.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import analyze_trends
import build_report
import common
import fetch_pageviews
import make_charts
import run_all

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
REPO_CACHE_DIR = Path(__file__).resolve().parents[1] / ".cache"

# Not the placeholder in common.DEFAULT_UA: `user_agent()` deliberately refuses
# a UA carrying "contact" or "example.org" with a SystemExit, so without this
# every test in the module would die at the UA check and prove nothing.
DESCRIPTIVE_UA = "wikipedia-trend-agent/0.1.0 (orchestrator-tests@invalid.example) python-urllib"

# A fixed clock, not `date.today()`: the asset's window ends 2026-09-20, and
# the fetch stage clamps the end to `today - 1 day`. A wall-clock `today` would
# silently change the number of chunks (and therefore the number of scripted
# requests) depending on the day the suite runs, so the pipeline would stop
# being a test of the committed asset and start being a test of the calendar.
FIXED_TODAY = date(2026, 9, 26)
FIXED_YESTERDAY = FIXED_TODAY - timedelta(days=1)

# The day (as an offset into the FIRST series' window) that carries a
# deliberate spike, so the anomaly detector has a real finding to draw and the
# overlay path is exercised rather than silently skipped.
SPIKE_OFFSET = 97


def _aqs_items(start_ymd: str, end_ymd: str, *, series_id: str, seed: int) -> bytes:
    """A 200 AQS body covering EXACTLY the inclusive chunk `[start_ymd, end_ymd]`.

    The view count varies across days and across the two series — a
    constant-per-chunk series would produce `anomaly_share == 0.0`, an empty
    overlay and a silently skipped branch of the chart stage — and one single
    day of the first series carries a large spike. Values stay well above
    1000/day so the confidence rubric scores volume and the report renders
    normally.
    """
    start = datetime.strptime(start_ymd, "%Y%m%d").date()
    end = datetime.strptime(end_ymd, "%Y%m%d").date()
    offset = 0
    items: list[dict[str, Any]] = []
    current = start
    while current <= end:
        views = 4200 + (current.toordinal() % 37) * 53 + seed * 613 + (current.toordinal() % 11) * 29
        if seed == 1 and offset == SPIKE_OFFSET:
            views = views * 9 + 5100
        items.append({"timestamp": f"{current.strftime('%Y%m%d')}00", "views": views})
        current += timedelta(days=1)
        offset += 1
    assert items, f"empty chunk {start_ymd}..{end_ymd} for {series_id}"
    return json.dumps({"items": items}, ensure_ascii=False).encode("utf-8")


def _chunk_stamps(url: str) -> tuple[str, str]:
    """The two 8-digit stamps in an AQS URL's `daily/<start>00/<end>00` segment."""
    segment = url.split("/daily/", 1)[1]
    start_ymd = segment.split("/", 1)[0][:8]
    end_ymd = segment.split("/", 1)[1][:8]
    return start_ymd, end_ymd


def _scripted_transport(transport_stub, spec_path: Path, *, fail_status: int | None = None):
    """A transport scripted for EXACTLY the requests the fetch stage will make.

    Walks `spec["series"]` in order and, per series, `chunk_ranges` of the
    clamped window, so the script lines up with the real request order (series
    one chunk one, series one chunk two, ...). With `fail_status` set the first
    scripted response is that status with an empty body and nothing follows it,
    because the fetch stage aborts on the first request and never asks for the
    rest. Returns `(stub, expected_call_count)`.
    """
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    window = spec["window"]
    clamped_end = fetch_pageviews.effective_end(window["end"], FIXED_YESTERDAY)
    responses: list[tuple[int, dict[str, str], bytes]] = []
    if fail_status is not None:
        responses.append((fail_status, {}, b""))
    else:
        for index, series in enumerate(spec["series"]):
            for start_ymd, end_ymd in fetch_pageviews.chunk_ranges(
                window["start"], clamped_end
            ):
                responses.append(
                    (
                        200,
                        {},
                        _aqs_items(start_ymd, end_ymd, series_id=str(series["id"]), seed=index + 1),
                    )
                )
    return transport_stub(responses), len(responses)


def _run(spec_path: Path, out_dir: Path, *, transport=None, today_utc=None) -> int:
    """The one and only place the orchestrated CLI is invoked.

    Every test goes through this, so the argv is never re-derived per test and
    a change to the orchestrator's CLI surface is a one-line change here.
    """
    return run_all.main(
        ["--spec", str(spec_path), "--out", str(out_dir)],
        transport=transport,
        today_utc=today_utc if today_utc is not None else FIXED_TODAY,
    )


def _isolated_cache(tmp_path: Path, monkeypatch) -> Path:
    """Repoint `common.CACHE_DIR` at `tmp_path` so the repo's `.cache/` is untouched.

    `common.CACHE_DIR` is bound at import time from `WTI_CACHE` and is consulted
    by `cache_path_for_key`, so rebinding the MODULE attribute is the only seam
    that works here: setting the environment variable after import would do
    nothing at all. Returns the new cache directory.
    """
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(common, "CACHE_DIR", cache_dir)
    return cache_dir


def _no_throttle(monkeypatch) -> None:
    """Make the four requests cost no wall-clock.

    `fetch_pageviews` calls `common.throttle(1.0, pace)` by attribute lookup,
    so rebinding the module attribute is the seam. The real throttle stays in
    the shipped path untouched — this is a test-only concern.
    """
    monkeypatch.setattr(common, "throttle", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def _orchestration_isolation(monkeypatch, tmp_path: Path) -> None:
    """Give every test in this module a descriptive UA, an isolated cache and no throttle."""
    monkeypatch.setenv("WTI_USER_AGENT", DESCRIPTIVE_UA)
    _isolated_cache(tmp_path, monkeypatch)
    _no_throttle(monkeypatch)


def _repo_cache_bytes() -> dict[str, bytes]:
    """Every file in the repository's own `.cache/`, so a write is detectable."""
    if not REPO_CACHE_DIR.is_dir():
        return {}
    return {path.name: path.read_bytes() for path in sorted(REPO_CACHE_DIR.iterdir()) if path.is_file()}


def _charts_png_count(out_dir: Path) -> int:
    """How many PNGs the chart stage actually rendered into `out_dir`."""
    return len(list(out_dir.glob("*.png")))


def test_the_example_asset_runs_the_whole_pipeline_with_no_network(
    tmp_out: Path, asset_spec_path: Path, transport_stub, capsys
) -> None:
    """The shipped asset goes through all four stages with no network at all.

    The positives come first (exit 0, the five artifacts, the request count, the
    User-Agent, the published documents), and only then the isolation claims, so
    a run that failed for a data reason cannot be mistaken for one that passed
    the isolation checks. `report.md` is read OFF DISK, never from
    `render_report`'s return value, for the reason `test_report._render_two_series`
    already documents.
    """
    spec = json.loads(asset_spec_path.read_text(encoding="utf-8"))
    stub, expected_calls = _scripted_transport(transport_stub, asset_spec_path)
    repo_cache_before = _repo_cache_bytes()

    assert _run(asset_spec_path, tmp_out, transport=stub) == 0

    for name in ("series.csv", "metrics.json", "charts.json", "report.md", "report.manifest.json"):
        assert tmp_out.joinpath(name).is_file(), f"missing artifact: {name}"

    assert len(stub.calls) == expected_calls
    for _url, headers in stub.calls:
        ua = headers.get("User-Agent", "")
        assert ua == DESCRIPTIVE_UA
        assert "example.org" not in ua and "contact" not in ua

    metrics = json.loads(tmp_out.joinpath("metrics.json").read_text(encoding="utf-8"))
    assert metrics["spec_name"] == spec["name"]
    assert [item["series_id"] for item in metrics["series"]] == [
        item["id"] for item in spec["series"]
    ]

    charts = json.loads(tmp_out.joinpath("charts.json").read_text(encoding="utf-8"))
    assert charts["contract_version"] == "charts.v1"
    assert len(charts["charts"]) == 2 * len(spec["series"]) + 1
    assert charts["charts"][-1]["kind"] == "overlay"

    manifest = json.loads(tmp_out.joinpath("report.manifest.json").read_text(encoding="utf-8"))
    assert manifest["contract_version"] == "report.v1"
    assert manifest["metrics_shown"]

    text = tmp_out.joinpath(build_report.REPORT_FILENAME).read_text(encoding="utf-8")
    tokens = build_report.report_tokens(str(spec["language"]))
    positions = [text.index(tokens[key]) for key in build_report.SECTION_TOKEN_KEYS]
    assert positions == sorted(positions), "the six headings are not in the frozen order"

    # Isolation: the repository's own cache is byte-unchanged, and the cache the
    # run actually used lives under `tmp_path`, not in the repository.
    assert _repo_cache_bytes() == repo_cache_before
    assert common.CACHE_DIR != REPO_CACHE_DIR
    assert REPO_CACHE_DIR not in common.CACHE_DIR.parents
    assert list(common.CACHE_DIR.glob("*.json")), "the run wrote no cache entry at all"
    captured = capsys.readouterr()
    assert "run_all: pipeline complete" in captured.out


def test_a_second_run_is_served_from_the_cache_and_makes_no_request(
    tmp_out: Path, asset_spec_path: Path, transport_stub
) -> None:
    """The cache is INSIDE the orchestrated path, not beside it.

    The second run deletes the report artifacts so the report stage must do real
    work again, and scripts the transport for ZERO responses: a second network
    request would raise inside the stub, and the report is republished from
    `metrics.json`/`charts.json` alone.
    """
    first, _ = _scripted_transport(transport_stub, asset_spec_path)
    assert _run(asset_spec_path, tmp_out, transport=first) == 0
    assert len(first.calls) > 0

    tmp_out.joinpath(build_report.REPORT_FILENAME).unlink()
    tmp_out.joinpath(build_report.REPORT_MANIFEST_FILENAME).unlink()

    second = transport_stub([])
    assert _run(asset_spec_path, tmp_out, transport=second) == 0
    assert len(second.calls) == 0
    assert tmp_out.joinpath(build_report.REPORT_FILENAME).is_file()
    assert tmp_out.joinpath(build_report.REPORT_MANIFEST_FILENAME).is_file()


def test_a_spec_problem_exits_two_and_names_the_stage(
    tmp_out: Path, tmp_path: Path, transport_stub, capsys
) -> None:
    """An invalid spec is exit 2, the stage is named, and nothing is written.

    Two violations on purpose (a dropped required field and an unknown root key)
    so the AGGREGATED stderr `common.load_and_validate_spec` prints is also
    exercised, not just its first line.
    """
    spec = json.loads((FIXTURES_DIR / "spec.example.json").read_text(encoding="utf-8"))
    spec.pop("language")
    spec["readme"] = "an unknown root key the contract must reject"
    spec_path = tmp_path / "invalid-spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")

    stub = transport_stub([])
    assert _run(spec_path, tmp_out, transport=stub) == 2

    captured = capsys.readouterr()
    assert "run_all: stage fetch failed: spec problem" in captured.err
    assert "spec.json validation failed (2 errors)" in captured.err
    assert not tmp_out.joinpath("series.csv").exists()
    assert len(stub.calls) == 0


def test_a_fetch_crash_exits_one_names_the_stage_and_writes_nothing_downstream(
    tmp_out: Path, asset_spec_path: Path, transport_stub, capsys
) -> None:
    """A 403 on the first request is exit 1, the stage is named, no artifact is written.

    The stage's OWN diagnostic is asserted too: an orchestrator that swallowed
    it would leave a model with an exit code and nothing to act on.
    """
    stub, _ = _scripted_transport(transport_stub, asset_spec_path, fail_status=403)

    assert _run(asset_spec_path, tmp_out, transport=stub) == 1

    captured = capsys.readouterr()
    assert "run_all: stage fetch failed: exit 1" in captured.err
    assert "HTTP 403" in captured.err
    assert "WTI_USER_AGENT" in captured.err
    for name in ("metrics.json", "charts.json", "report.md", "report.manifest.json"):
        assert not tmp_out.joinpath(name).exists(), f"{name} was written despite a fetch crash"


def test_a_later_stage_failure_stops_the_run_at_that_stage(
    tmp_out: Path, asset_spec_path: Path, transport_stub, monkeypatch, capsys
) -> None:
    """A failure at `analyze` leaves every later artifact exactly as it was.

    The condition the plan names is "`series.csv` is missing when `analyze`
    starts". Deleting the file and re-running does NOT create it: the second
    `fetch` succeeds (from cache) and republishes `series.csv`, so the run
    would return 0 and prove nothing. The fetch stage's CSV WRITE is therefore
    neutralised for the second run only, which creates exactly the named
    condition without touching a contract or a shipped path.

    "No later stage runs" is asserted by the ABSENCE OF A REWRITE: the chart
    manifest and the report are read before the second run and compared byte for
    byte after it.
    """
    stub, _ = _scripted_transport(transport_stub, asset_spec_path)
    assert _run(asset_spec_path, tmp_out, transport=stub) == 0
    charts_before = tmp_out.joinpath("charts.json").read_bytes()
    report_before = tmp_out.joinpath(build_report.REPORT_FILENAME).read_bytes()

    monkeypatch.setattr(
        fetch_pageviews, "_write_series_csv", lambda out_dir, rows: Path(out_dir) / "series.csv"
    )
    tmp_out.joinpath("series.csv").unlink()
    second = transport_stub([])

    assert _run(asset_spec_path, tmp_out, transport=second) == 1

    captured = capsys.readouterr()
    assert "run_all: stage analyze failed: exit 1" in captured.err
    assert "could not read series CSV" in captured.err or "analysis failed" in captured.err
    assert tmp_out.joinpath("charts.json").read_bytes() == charts_before
    assert tmp_out.joinpath(build_report.REPORT_FILENAME).read_bytes() == report_before


def test_a_report_refused_language_does_not_skip_the_report_stage(
    tmp_out: Path, tmp_path: Path, asset_spec_path: Path, transport_stub, capsys
) -> None:
    """CONTRACTS.md §8.3's named Phase 7 constraint, end to end.

    §8.3: "the chart stage accepts `ja`, and this table does not. A `ja` spec
    therefore produces a valid `charts.json` — with a disclosed tofu-box font
    gap — and is then refused at the report stage with exit 1 and nothing
    written. That is the correct answer, and this paragraph is the named Phase 7
    constraint: `run_all.py` must not treat a successful chart stage as a
    report-stage precondition, because the two stages do not accept the same
    language set in v1."

    So the run MUST reach the report stage, MUST fail there, and the charts it
    already published MUST stay on disk. A "skip the report stage when the
    language is not shared" optimisation would break exactly this: it would
    name `charts` on stderr and report a code the model would misread.
    """
    spec = json.loads(asset_spec_path.read_text(encoding="utf-8"))
    spec["language"] = "ja"
    spec_path = tmp_path / "ja-spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")

    stub, _ = _scripted_transport(transport_stub, spec_path)
    assert _run(spec_path, tmp_out, transport=stub) == 1

    captured = capsys.readouterr()
    assert "run_all: stage report failed: exit 1" in captured.err
    assert "run_all: stage charts failed" not in captured.err
    assert "no report tokens for language: ja" in captured.err

    charts = json.loads(tmp_out.joinpath("charts.json").read_text(encoding="utf-8"))
    assert len(charts["charts"]) == 2 * len(spec["series"]) + 1
    assert _charts_png_count(tmp_out) == 2 * len(spec["series"]) + 1
    assert not tmp_out.joinpath(build_report.REPORT_FILENAME).exists()
    assert not tmp_out.joinpath(build_report.REPORT_MANIFEST_FILENAME).exists()


def test_run_all_reads_no_pipeline_artifact_and_catches_no_broad_exception() -> None:
    """The ANAL-06 never-recompute rule reaches the orchestrator, enforced by source.

    The positive half matters as much as the negative one: the module DOES
    import all four stage modules and DOES mention `SystemExit`, so a file that
    simply never ran a stage cannot satisfy this test vacuously.
    """
    source = Path(str(run_all.__file__)).read_text(encoding="utf-8")

    for forbidden in (
        "import json",
        "json.loads",
        '"metrics.json"',
        '"series.csv"',
        '"charts.json"',
        "except Exception",
        "except:",
        "subprocess",
    ):
        assert forbidden not in source, f"run_all.py must not contain {forbidden!r}"

    for module_name in ("fetch_pageviews", "analyze_trends", "make_charts", "build_report"):
        assert f"import {module_name}" in source
    assert "SystemExit" in source
    assert [name for name, _ in run_all.STAGES] == ["fetch", "analyze", "charts", "report"]


def test_a_non_descriptive_user_agent_surfaces_as_a_non_zero_exit(
    tmp_out: Path, asset_spec_path: Path, monkeypatch, capsys
) -> None:
    """The `user_agent()` fail-fast surfaces as a non-zero exit naming `fetch`.

    `common.user_agent()` raises `SystemExit` with a STRING message (a model
    action, not a number), so `run_all` maps it to 1 — never to 0, and never to
    the 2 that means "fix the spec". Pinned after observation.
    """
    monkeypatch.delenv("WTI_USER_AGENT", raising=False)

    code = _run(asset_spec_path, tmp_out, transport=None)

    assert code == 1
    captured = capsys.readouterr()
    assert "run_all: stage fetch failed: exited 1" in captured.err
    assert "invalid_ua" in captured.err
    assert not tmp_out.joinpath("series.csv").exists()


def test_the_orchestrator_adds_no_third_party_import() -> None:
    """`run_all.py` gains no third-party import, and no new runtime dependency.

    matplotlib arrives transitively when `make_charts` is imported and nothing
    in the orchestrator needs it directly, so the shipped dependency budget is
    unchanged: `requirements.txt` stays the single line `matplotlib>=3.11`.
    """
    import ast

    source = Path(str(run_all.__file__)).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "matplotlib" not in imported
    assert "numpy" not in imported
    assert "pandas" not in imported
    assert imported <= {
        "__future__",
        "argparse",
        "sys",
        "collections",
        "datetime",
        "types",
        "typing",
        "common",
        "analyze_trends",
        "build_report",
        "fetch_pageviews",
        "make_charts",
    }

    requirements = Path(str(run_all.__file__)).resolve().parents[1].joinpath("requirements.txt")
    assert [line.strip() for line in requirements.read_text(encoding="utf-8").splitlines() if line.strip()] == [
        "matplotlib>=3.11"
    ]


def test_the_orchestrator_cli_offers_exactly_three_flags(tmp_path: Path, capsys) -> None:
    """`--spec`, `--out` and `--verbose` only, and nothing else.

    A `--stages` subset switch would let a caller publish a `report.md` from a
    `charts.json` it did not render; `--ttl-hours` and `--log-scale` already
    belong to the stage CLIs that own them. The refusal half is what makes the
    positive half meaningful: an unknown flag must be rejected by argparse, not
    quietly absorbed.
    """
    accepted = tmp_path / "accepted.json"
    accepted.write_text("{}", encoding="utf-8")

    # The three documented flags are accepted: the run reaches the spec
    # validator and fails there, not in argparse.
    assert run_all.main(["--spec", str(accepted), "--out", str(tmp_path), "--verbose"]) == 2
    accepted_capture = capsys.readouterr()
    assert "unrecognized arguments" not in accepted_capture.err
    assert "run_all: stage fetch failed: spec problem" in accepted_capture.err

    for flag in ("--ttl-hours", "--log-scale", "--stages"):
        with pytest.raises(SystemExit) as excinfo:
            run_all.main(["--spec", str(accepted), "--out", str(tmp_path), flag, "1"])
        assert excinfo.value.code == 2
        assert "unrecognized arguments" in capsys.readouterr().err


def test_a_stage_argv_carries_the_same_spec_and_out_to_every_stage(
    tmp_out: Path, asset_spec_path: Path, transport_stub
) -> None:
    """All four stages receive the SAME `--spec` and the SAME `--out`.

    Proven from the artifacts rather than from a mock: the metrics document's
    `generated_from` names the spec the analysis stage was handed, and the
    report manifest names the metrics path inside `tmp_out` that the report
    stage read.
    """
    stub, _ = _scripted_transport(transport_stub, asset_spec_path)
    assert _run(asset_spec_path, tmp_out, transport=stub) == 0

    metrics = json.loads(tmp_out.joinpath("metrics.json").read_text(encoding="utf-8"))
    assert metrics["generated_from"] == str(asset_spec_path)

    manifest = json.loads(tmp_out.joinpath("report.manifest.json").read_text(encoding="utf-8"))
    assert str(manifest["generated_from"]).endswith(str(tmp_out / "metrics.json"))


def test_a_partial_fetch_exit_three_stops_before_analyze(
    tmp_out: Path, asset_spec_path: Path, transport_stub, capsys
) -> None:
    """A `3` from fetch is passed through unchanged and analyze never runs.

    The first series answers both of its chunks with 200; the second series
    answers both with a non-retryable 400, which the fetch stage counts as a
    failed series and reports as the partial exit 3. A `series.csv` missing a
    series is the analyzer's own refusal to discover, so continuing would only
    trade a named failure for an unnamed one.

    400 rather than 503 on purpose: 503 is retryable, so the stage would make
    three attempts per chunk against a stub scripted for one.
    """
    spec = json.loads(asset_spec_path.read_text(encoding="utf-8"))
    window = spec["window"]
    clamped_end = fetch_pageviews.effective_end(window["end"], FIXED_YESTERDAY)
    chunks = fetch_pageviews.chunk_ranges(window["start"], clamped_end)
    assert len(chunks) == 2

    first_series = [
        (200, {}, _aqs_items(start, end, series_id=str(spec["series"][0]["id"]), seed=1))
        for start, end in chunks
    ]
    second_series = [(400, {}, b"") for _ in chunks]
    stub = transport_stub(first_series + second_series)

    code = _run(asset_spec_path, tmp_out, transport=stub)

    assert code == 3
    assert len(stub.calls) == 4
    captured = capsys.readouterr()
    assert "run_all: stage fetch failed: exit 3" in captured.err
    assert not tmp_out.joinpath("metrics.json").exists()
    assert not tmp_out.joinpath("charts.json").exists()
    assert not tmp_out.joinpath("report.md").exists()


def test_every_stage_main_is_reached_in_order(tmp_out: Path, asset_spec_path: Path, monkeypatch) -> None:
    """The four stages run in the order `STAGES` names, and only those four.

    Wrapping each stage module's `main` in a recorder proves the ORDER and the
    FACT that the later stages were reached — the properties a source-level
    reading of the loop cannot establish. The wrappers call through, so the run
    is still the real pipeline.
    """
    spec = json.loads(asset_spec_path.read_text(encoding="utf-8"))
    responses: list[tuple[int, dict[str, str], bytes]] = []
    window = spec["window"]
    clamped_end = fetch_pageviews.effective_end(window["end"], FIXED_YESTERDAY)
    for index, series in enumerate(spec["series"]):
        for start, end in fetch_pageviews.chunk_ranges(window["start"], clamped_end):
            responses.append((200, {}, _aqs_items(start, end, series_id=str(series["id"]), seed=index + 1)))
    seen: list[str] = []

    for stage_name, module_name in run_all.STAGES:
        module = run_all._STAGE_MODULES[module_name]
        original = getattr(module, "main")

        def recording(argv, *, _name=stage_name, _original=original, **kwargs):
            seen.append(_name)
            return _original(argv, **kwargs)

        monkeypatch.setattr(module, "main", recording)

    stub = _Transport(responses)
    assert _run(asset_spec_path, tmp_out, transport=stub) == 0
    assert seen == ["fetch", "analyze", "charts", "report"]


class _Transport:
    """A minimal recording transport for the order-recording test."""

    def __init__(self, responses: list[tuple[int, dict[str, str], bytes]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float = 30.0):
        from conftest import StubResponse

        self.calls.append((url, dict(headers)))
        if not self._responses:
            raise AssertionError(f"transport called {len(self.calls)} times with nothing scripted")
        status, response_headers, body = self._responses.pop(0)
        return StubResponse(status, response_headers, body)


def test_a_first_stage_failure_prevents_every_later_stage(
    tmp_out: Path, asset_spec_path: Path, transport_stub, monkeypatch
) -> None:
    """A failure at the FIRST stage reaches no other stage.

    The complement of the order test: it proves the loop really stops, rather
    than running a stage and discarding its result.
    """
    seen: list[str] = []
    for stage_name, module_name in run_all.STAGES:
        module = run_all._STAGE_MODULES[module_name]
        original = getattr(module, "main")

        def recording(argv, *, _name=stage_name, _original=original, **kwargs):
            seen.append(_name)
            return _original(argv, **kwargs)

        monkeypatch.setattr(module, "main", recording)

    stub, _ = _scripted_transport(transport_stub, asset_spec_path, fail_status=403)
    assert _run(asset_spec_path, tmp_out, transport=stub) == 1
    assert seen == ["fetch"]


def test_the_analysis_stage_consumes_the_fetch_stage_csv_and_the_report_the_metrics(
    tmp_out: Path, asset_spec_path: Path, transport_stub
) -> None:
    """The four artifacts form a chain: each stage's input is the previous one's output.

    `analyze_trends.load_series_csv` is the reader both the analysis and the
    chart stage use, so a CSV the fetch stage wrote that neither can read would
    surface as a non-zero `analyze` — this asserts the chain positively by
    reading the CSV back with the same reader the pipeline uses.
    """
    spec = json.loads(asset_spec_path.read_text(encoding="utf-8"))
    stub, _ = _scripted_transport(transport_stub, asset_spec_path)
    assert _run(asset_spec_path, tmp_out, transport=stub) == 0

    grouped = analyze_trends.load_series_csv(tmp_out / "series.csv", spec)
    assert sorted(grouped) == sorted(str(item["id"]) for item in spec["series"])

    metrics, _sha = make_charts.load_metrics(tmp_out / "metrics.json")
    assert metrics["spec_name"] == spec["name"]
