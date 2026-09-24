---
name: wikipedia-trend-agent
description: Analyzes Wikipedia pageviews trends for topics and language editions via the Wikimedia Pageviews API: compares interest across series, measures growth (3M/1Y/2Y with spike-excluded variants), detects seasonality and anomalies, and produces a metrics.json single source of truth plus a Markdown report with PNG charts. Use when the user asks whether interest in a topic or language is growing («чи зростає інтерес до теми X»), wants a comparison across languages/topics, or needs to know how much a trend can be trusted. Runs on cheap/free models — stdlib-thin, one runtime dependency.
license: Apache-2.0
compatibility: "Python >= 3.11; HTTPS access to wikimedia.org"
metadata:
  version: 0.1.0
  spec-url: https://agentskills.io/specification
  data-source: Wikimedia Pageviews API
---

# Wikipedia Trend Agent

Model writes one `spec.json`; the pipeline returns `metrics.json` plus a
Markdown report with PNG charts. Never invent numbers: every figure in an
answer comes from `out/metrics.json`.

## When to use

Use this skill when the user:

- asks whether interest in a topic or language edition is growing
  («чи зростає інтерес до теми X»);
- compares interest across language editions or topics
  («порівняй інтерес у мовних розділах»);
- asks which topic or language to develop next;
- wants to know how much a trend can be trusted (confidence, anomalies,
  seasonality).

Do not use it as a demand/ability-to-pay forecast: article interest ≠ product
demand. Say that explicitly in conclusions.

## Workflow

1. **Author `spec.json`.** Turn the request into the frozen input contract:
   articles-first `series[]` with full fields per `references/CONTRACTS.md`
   (§1). The topic lives only in `request`.
2. **Validate.** Run the spec through the shared validator (aggregated model-
   readable stderr, exit 2 on violations). Example spec:
   `tests/fixtures/spec.example.json`.
3. **Run the stages** — these land in later phases (roadmap, not yet all here):
   - fetch (`fetch_pageviews.py`) — data → `series.csv` with `.cache/`,
     UA fail-fast and ~1 req/s throttle;
   - analyze (`analyze_trends.py`) — growth/seasonality/anomalies/confidence
     → `metrics.json`;
   - charts (`make_charts.py`) — PNG charts;
   - report (`build_report.py`) — Markdown report;
   - **one-command `run_all.py` arrives in v0.7** — not implemented yet; until
     then run the landed stages individually.
4. **Answer from `metrics.json` only.** Copy `confidence`, `confidence_reasons`,
   `as_of` and growth numbers verbatim; cite the charts.

## Policies

- **User-Agent:** set `WTI_USER_AGENT` to a descriptive value before any
  network call (Wikimedia 403s without it). Placeholder UAs fail fast with an
  actionable error — a non-descriptive identity never leaks.
- **Cache:** `WTI_CACHE` (default `.cache/`); TTL `WTI_TTL_HOURS` (default 24).
- **Throttle:** ~1 request/second; on 429 respect `Retry-After`.
- **No edits:** the skill only reads public statistics and search metadata
  (`WP:BOT` / bot policy does not apply — there is no write).
- **Contracts:** never emit or read fields absent from `references/CONTRACTS.md`
  — pipeline stages must not break the frozen v0.1 contract.

## Red flags (stop and warn the user)

- Empty series or spec contract violations → exit 2 with model-readable
  stderr; fix the spec, do not rewrite code.
- `pct: null` in `metrics.json` means *not computable* — never read it as 0
  growth, and never report it as a number (ANAL-06).
- < 1000 views/month — noise, not signal.
- Comparison crossing 2015-05-01 — methodology change (spider/bot filtering).
- One giant spike driving the whole trend — check `anomalies` in `metrics.json`.

## Verification checklist

- [ ] `metrics.json` exists and every number in the answer matches it.
- [ ] `as_of` stated (last complete day of data).
- [ ] `confidence` + `confidence_reasons` copied verbatim.
- [ ] Assumptions and limitations listed; `low` confidence phrased as a
      hypothesis, never a conclusion.
- [ ] Contracts hold and tests are green:
      `cd wikipedia-trend-agent && python -m pytest -q`.

## Performance on cheap model

- One input file (`spec.json`), one output file to read (`metrics.json`).
- All arithmetic lives in code — the model quotes numbers, never recomputes.
- Stdlib-first: the only runtime dependency is `matplotlib>=3.11` (charts).
- If a stage crashes: read exit code + stderr, fix the spec, do not rewrite
  pipeline logic.