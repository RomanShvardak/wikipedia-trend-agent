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
import build_pdf
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
    # RUN-03 was relaxed from three example specs to ONE, so the DIRECTORY is
    # asserted, not just the file: a second hand-written variant would parse
    # perfectly and this is the only assertion that can see it.
    example_specs = sorted(
        path.name
        for path in asset_spec_path.parent.glob("example.*.json")
        if path.is_file()
    )
    assert example_specs == [asset_spec_path.name]
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

    `build_pdf` joined this whitelist in Phase 9 and the claim is UNCHANGED, which is the
    point of the closed list: it enumerates the orchestrator's first-party SIBLINGS, not its
    dependencies. The PDF stage's optional `pypandoc` is imported lazily inside
    `build_pdf.render_pdf` precisely so that importing the orchestrator costs nothing — a
    module-level import would make the whole skill require the optional toolchain, which is
    the one thing Phase 9 promised it would not do.
    `test_build_pdf.test_the_renderer_imports_pypandoc_lazily_and_never_asks_for_a_root`
    holds the laziness.
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
    # The PDF toolchain specifically. An orchestrator that imported `pypandoc` would make
    # the WHOLE skill require the optional toolchain, which is the promise this phase makes.
    assert "pypandoc" not in imported, (
        "the orchestrator must not import pypandoc; the PDF stage imports it lazily so that "
        "`--pdf` stays opt-in and the base install stays one package"
    )
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
        "build_pdf",
        "build_report",
        "fetch_pageviews",
        "make_charts",
    }

    requirements = Path(str(run_all.__file__)).resolve().parents[1].joinpath("requirements.txt")
    assert [line.strip() for line in requirements.read_text(encoding="utf-8").splitlines() if line.strip()] == [
        "matplotlib>=3.11"
    ]


def test_the_three_shared_flags_plus_one_stage_switch_are_accepted(
    tmp_path: Path, capsys
) -> None:
    """The three flags every stage shares, plus `--pdf`, and nothing else.

    RENAMED in Phase 9 from `test_the_orchestrator_cli_offers_exactly_three_flags`, and the
    rename is the point rather than a cosmetic edit. The old test argued that a fourth flag
    "would be a contract the stages do not own" — and the reasoning HELD, so the argument
    was kept and the count changed. `--pdf` switches on a whole STAGE; `--ttl-hours` and
    `--log-scale` configure one stage and stay off this parser; a `--stages` subset switch
    would still let a caller publish a `report.md` from a `charts.json` it did not render, and
    is still refused below.

    A test whose NAME states a rule the code deliberately breaks teaches the next reader to
    delete the rule, so the name moved with the behaviour and
    `test_the_orchestrator_no_longer_claims_exactly_three_flags` holds it there.

    The refusal half is what makes the positive half meaningful: an unknown flag must be
    rejected by argparse, not quietly absorbed.
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


# The two articles the shipped example measures, pinned because the slugs are
# LIVE facts and a slug that 404s is a production defect, not a cosmetic one.
#
# HISTORY. The asset originally carried `Post_przerywany` (pl) and
# `P%C5%AFst_p%C5%99eru%C5%A1ovan%C3%BD` (cs). Both return 404 from AQS and are
# reported `missing` by the Action API: pl.wikipedia has no intermittent-fasting
# article at all, and the Czech title had its two words transposed. Every stage
# still exited 0 over a mocked transport, so nothing in the suite could see it -
# which is exactly why the two values are pinned here rather than left to the
# fixture corpus, whose series are a synthetic ramp and whose article column is a
# label on invented numbers, not a claim about a live page.
VERIFIED_ARTICLES = {
    "pl-post-przerywany": "G%C5%82od%C3%B3wka_lecznicza",  # Głodówka lecznicza, pageid 74017
    "cs-pust-prerusovany": "P%C5%99eru%C5%A1ovan%C3%BD_p%C5%AFst",  # Přerušovaný půst, pageid 1632302
}

# The 404 slugs the asset used to carry, kept as an explicit deny-list so the
# correction cannot be reverted by copying the old value back in.
REJECTED_ARTICLES = ("Post_przerywany", "P%C5%AFst_p%C5%99eru%C5%A1ovan%C3%BD")


def test_the_asset_names_the_two_articles_that_actually_exist(asset_spec_path: Path) -> None:
    """The shipped example's slugs are the probe-verified existing ones.

    Neither value is fetched here - no test may reach the network - so the
    claim being pinned is the one the suite can still make offline: the asset
    carries exactly these two slugs. They were verified live against the Action
    API (`action=query&titles=`, both `ns: 0` with a pageid) and against AQS
    (both HTTP 200).
    """
    spec = json.loads(asset_spec_path.read_text(encoding="utf-8"))
    by_id = {str(item["id"]): str(item["article"]) for item in spec["series"]}

    assert by_id == VERIFIED_ARTICLES, (
        "the shipped example's article slugs changed; the asset is the file a "
        f"reader runs live, so a slug here is a production fact. Expected "
        f"{VERIFIED_ARTICLES}, found {by_id}"
    )
    for series_id, article in by_id.items():
        assert article not in REJECTED_ARTICLES, (
            f"{series_id} is back to a slug Wikimedia reports as missing: {article}"
        )
        # `resolve_articles.py` emits `quote(title, safe='')`, so a spec slug
        # the resolver could have produced is percent-encoded. Asserting the
        # shape keeps the asset copy-pasteable straight out of a resolver run.
        assert "%" in article, (
            f"{series_id}'s slug is not percent-encoded, so it is not what "
            f"resolve_articles.py emits: {article}"
        )


def test_the_asset_discloses_that_the_two_series_are_not_the_same_topic(
    asset_spec_path: Path,
) -> None:
    """The topic asymmetry is stated in the spec, not discovered by the reader.

    `pl.wikipedia` has no intermittent-fasting article; `Głodówka lecznicza`
    covers fasting generally, so the comparison measures adjacent topics. That
    is a caveat about what the numbers MEAN, so it belongs in the `assumptions`
    the report prints - and a report that is silent about it would be asserting
    a like-for-like comparison the data does not support.
    """
    spec = json.loads(asset_spec_path.read_text(encoding="utf-8"))
    assumptions = " ".join(str(item) for item in spec["assumptions"])

    assert "pl.wikipedia" in assumptions, (
        "the assumptions no longer name which wiki lacks the dedicated article"
    )
    assert "Głodówka lecznicza" in assumptions, (
        "the assumptions do not name the article actually measured on pl.wikipedia"
    )
    assert any(
        "pl.wikipedia" in str(item) for item in spec["assumptions"]
    ), "no single assumption carries the pl.wikipedia asymmetry"

    # The pl label must not claim to be the intermittent article it is not: it
    # is carried into chart titles and the report table, where a reader meets it
    # before they meet the assumptions.
    pl = next(item for item in spec["series"] if item["id"] == "pl-post-przerywany")
    assert "інтервальне голодування" not in str(pl["label"]), (
        "the pl label still claims to be intermittent fasting, but the article "
        f"measured is fasting in general: {pl['label']}"
    )


def test_the_fetch_stage_sends_each_asset_slug_unmodified(
    tmp_out: Path, asset_spec_path: Path, transport_stub
) -> None:
    """The percent-encoded spec slug reaches the AQS URL byte for byte.

    `fetch_pageviews.series_url` interpolates the spec's `article` into the
    request path with no quoting, which is correct ONLY because the resolver
    already percent-encoded it. That is an invariant nothing else states: the
    mocked transport is indifferent to what the URL contains, and the live run
    cannot run in CI. So the pass-through is asserted here directly - each
    requested URL must contain the spec's own slug, with its `%XX` triplets
    intact and not double-encoded.
    """
    spec = json.loads(asset_spec_path.read_text(encoding="utf-8"))
    stub, expected_calls = _scripted_transport(transport_stub, asset_spec_path)
    assert _run(asset_spec_path, tmp_out, transport=stub) == 0

    requested = [url for url, _headers in stub.calls]
    assert len(requested) == expected_calls
    for series in spec["series"]:
        article = str(series["article"])
        matching = [url for url in requested if f"/{article}/daily/" in url]
        assert matching, (
            f"no AQS request carried the spec slug {article!r} verbatim; the "
            f"fetch stage must not re-encode or decode what the resolver gave it"
        )
        for url in matching:
            assert str(series["project"]) in url
            assert "%25" not in url, (
                "the slug was double-encoded on its way into the request URL: "
                f"{url}"
            )


# ============================ wave 5: the orchestrator's opt-in PDF stage (T13-T17)


# --- Phase 9: the opt-in PDF stage's integration tests need the optional toolchain -----
#
# They SKIP with a reason naming both install commands rather than failing, and they skip
# ONLY the ones that genuinely cannot run. The offline half of the orchestrator contract --
# no `--pdf` changes nothing, a failing report stops the run, a failing PDF fails the
# pipeline -- is asserted with NO toolchain at all, so the default suite still proves the
# feature is opt-in on a machine that never installed it.
def _pdf_toolchain_available() -> bool:
    import shutil

    try:
        import pypandoc  # noqa: F401
    except ImportError:
        return False
    return shutil.which("typst") is not None


needs_toolchain = pytest.mark.skipif(
    not _pdf_toolchain_available(),
    reason="optional PDF toolchain absent: pip install -r requirements-pdf.txt, then "
           "winget install Typst.Typst",
)


def _offline_fetch(argv: list[str], **_ignored: object) -> int:
    """A `fetch_pageviews.main` stand-in that publishes the committed fixture series.

    Every orchestrator test below is about ROUTING and about what the flag does to the
    output directory, not about HTTP. Scripting the transport for that would be a lot of
    machinery to prove a stage order, and the real fetch already runs end to end in
    `test_the_shipped_asset_runs_end_to_end_with_no_network`. This writes the one file the
    downstream stages read, so analyze, charts, report and pdf all run for real.

    It takes the stage's own `argv` list, exactly as `run_all` invokes every stage, and reads
    `--out` out of it — so the stand-in cannot drift from the calling convention it replaces.
    """
    out_dir = Path(argv[argv.index("--out") + 1])
    (out_dir / "series.csv").write_bytes(
        (FIXTURES_DIR / "series.example.csv").read_bytes()
    )
    return 0


@pytest.fixture
def offline_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the fetch stage with `_offline_fetch` for the rest of the test."""
    monkeypatch.setattr(fetch_pageviews, "main", _offline_fetch)


def _pipeline_argv(out_dir: Path, *extra: str) -> list[str]:
    return [
        "--spec", str(FIXTURES_DIR / "spec.example.json"), "--out", str(out_dir), *extra,
    ]


def test_the_stage_list_is_unchanged_and_the_optional_one_is_separate() -> None:
    """T14. `STAGES` is still exactly the four core stages.

    The whole reason `OPTIONAL_STAGES` is a separate registry. An implementation that
    appended `("pdf", "build_pdf")` to `STAGES` would satisfy every behavioural test and
    break the frozen contract this asserts — the pipeline's order is not a bag features are
    poured into.
    """
    assert [name for name, _ in run_all.STAGES] == ["fetch", "analyze", "charts", "report"]
    assert [name for name, _, _ in run_all.OPTIONAL_STAGES] == ["pdf"]
    assert [flag for _, _, flag in run_all.OPTIONAL_STAGES] == ["--pdf"]
    assert "build_pdf" not in {module for _, module in run_all.STAGES}
    assert [module for _, module, _ in run_all.OPTIONAL_STAGES] == ["build_pdf"]


def test_the_orchestrator_parser_offers_the_three_shared_flags_plus_pdf() -> None:
    """T13, the parser half. Four flags, and the fourth is the stage switch.

    The three shared ones exist for every stage; `--pdf` is consumed by the loop and never
    forwarded, which is the same ownership split that keeps `--ttl-hours` and `--log-scale`
    off this parser entirely.
    """
    defined = {
        action.option_strings[0]
        for action in run_all._parser()._actions
        if action.option_strings and action.option_strings[0] != "-h"
    }
    assert defined == {"--spec", "--out", "--verbose", "--pdf"}, (
        f"the orchestrator's flag set drifted: {sorted(defined)}"
    )


def test_without_the_pdf_flag_the_output_directory_is_byte_identical(
    tmp_out: Path, capsys, offline_pipeline: None
) -> None:
    """T13, the load-bearing half, proven with sha256 over EVERY file.

    The claim is not "the PDF stage is not called" — it is "a run without `--pdf` publishes
    exactly what it published before this phase, byte for byte". That is the promise that
    makes the feature safe to ship, and hashing the directory is the only way to check it: a
    new file, a rewritten manifest, a reordered array, anything.

    Two full pipelines run into the SAME directory sequentially, because `generated_from`
    records the `metrics.json` path and two directories would differ in exactly the field
    being compared.
    """
    import hashlib

    assert run_all.main(_pipeline_argv(tmp_out)) == 0
    capsys.readouterr()

    def snapshot() -> dict[str, str]:
        return {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(tmp_out.iterdir()) if p.is_file()
        }

    before = snapshot()
    assert before, "the baseline run published nothing to compare against"
    assert build_pdf.PDF_FILENAME not in before, "the precondition: no PDF yet"
    assert "report.manifest.json" in before

    assert run_all.main(_pipeline_argv(tmp_out)) == 0
    stdout = capsys.readouterr().out
    after = snapshot()
    assert set(after) == set(before), (
        f"a run without --pdf changed which files exist: "
        f"added {sorted(set(after) - set(before))}, "
        f"removed {sorted(set(before) - set(after))}"
    )
    changed = sorted(k for k in before if before[k] != after[k])
    assert not changed, f"a run without --pdf rewrote {changed}"
    assert "Wrote PDF" not in stdout, f"the default path must not publish a PDF: {stdout!r}"


@needs_toolchain
def test_the_pdf_flag_runs_the_pdf_stage_last_and_publishes(
    tmp_out: Path, capsys, offline_pipeline: None
) -> None:
    """T15. `--pdf` publishes a PDF, and the manifest records it.

    The real analyze, charts, report and pdf stages all run; only the fetch is stood in for.
    """
    assert run_all.main(_pipeline_argv(tmp_out)) == 0
    manifest_path = tmp_out / build_report.REPORT_MANIFEST_FILENAME
    before = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert before["formats"] == build_report.RATIFIED_V1_FORMATS, "precondition"

    assert run_all.main(_pipeline_argv(tmp_out, "--pdf")) == 0
    stdout = capsys.readouterr().out
    assert "Wrote PDF" in stdout, f"the pipeline must report the PDF it published: {stdout!r}"
    assert (tmp_out / build_pdf.PDF_FILENAME).read_bytes()[:5] == b"%PDF-"

    after = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert after["formats"] == [
        *build_report.RATIFIED_V1_FORMATS, build_report.RATIFIED_PDF_FORMAT,
    ]
    assert after["contract_version"] == "report.v1"
    assert set(after) == set(before), "no top-level key may appear or vanish"
    assert build_report.report_is_stale(tmp_out) is False


def test_a_failing_report_stage_means_the_pdf_stage_never_runs(
    tmp_out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T16. First non-zero stops the run, for the optional stage too.

    `build_pdf` is last precisely because it reads `report.manifest.json`. A pipeline that
    carried on to render a PDF after the report stage failed would publish a document whose
    manifest was never written — a file no consumer could resolve.
    """
    import build_pdf as build_pdf_module

    calls: list[str] = []

    def recorder(name: str, code: int):
        def run(argv: list[str], **_ignored: object) -> int:
            calls.append(name)
            return code
        return run

    monkeypatch.setattr(fetch_pageviews, "main", recorder("fetch", 0))
    monkeypatch.setattr(analyze_trends, "main", recorder("analyze", 0))
    monkeypatch.setattr(make_charts, "main", recorder("charts", 0))
    monkeypatch.setattr(build_report, "main", recorder("report", 3))
    monkeypatch.setattr(build_pdf_module, "main", recorder("pdf", 0))

    code = run_all.main(_pipeline_argv(tmp_out, "--pdf"))
    assert code == 3, f"the report stage's exit code must pass through unchanged, got {code}"
    assert calls == ["fetch", "analyze", "charts", "report"], (
        f"the pdf stage ran after a failing report stage: {calls}"
    )


@needs_toolchain
def test_a_failing_pdf_stage_fails_the_pipeline_with_a_named_message(
    tmp_out: Path, capsys, monkeypatch: pytest.MonkeyPatch, offline_pipeline: None
) -> None:
    """T17. A PDF failure is a REAL failure, not a warning.

    The tempting design — warn, exit 0, because `report.md` is already written — would make
    `--pdf` a flag whose failure is invisible, and a caller who asked for a document would
    be told the pipeline succeeded while holding none. That is the one outcome this test
    exists to prevent.
    """
    import build_pdf as build_pdf_module

    assert run_all.main(_pipeline_argv(tmp_out)) == 0
    monkeypatch.setattr(build_pdf_module, "main", lambda argv, **_: 1)
    code = run_all.main(_pipeline_argv(tmp_out, "--pdf"))
    assert code == 1
    assert "run_all: stage pdf failed: exit 1" in capsys.readouterr().err


def test_the_orchestrator_no_longer_claims_exactly_three_flags() -> None:
    """The stale name is gone, and stays gone.

    `test_the_orchestrator_cli_offers_exactly_three_flags` argued that a fourth flag "would
    be a contract the stages do not own". Phase 9 added one on purpose and the reasoning
    held, so the test was RENAMED rather than deleted or left to rot. A test whose name
    states a rule the code deliberately breaks teaches the next reader to delete the rule, so
    this asserts the rename is in place.
    """
    import inspect

    import test_run_all

    names = {n for n, _ in inspect.getmembers(test_run_all, inspect.isfunction)}
    assert "test_the_orchestrator_cli_offers_exactly_three_flags" not in names, (
        "the 'exactly three flags' test must be renamed: run_all defines four now, and the "
        "old name states a rule the code deliberately breaks"
    )
    assert "test_the_three_shared_flags_plus_one_stage_switch_are_accepted" in names