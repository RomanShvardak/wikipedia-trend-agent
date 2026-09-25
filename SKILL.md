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

1. **Discover canonical articles.** Run `resolve_articles.py` with the shared
   `--topic`, ordered `--projects`, and any repeatable `--topic-for
   PROJECT=QUERY` overrides. Inspect `out/resolved.json` (`resolved.v1`); never
   treat search ranking as confirmation.
2. **Inspect the candidate evidence.** Read each project's `search_hits`,
   redirect provenance, 30-day AQS volume state, `ready`/`ambiguous` status,
   and recommendation. A zero-hit or structurally unresolved project is not
   confirmable.
3. **Confirm offline.** Repeat the exact topic, project order, and every
   `--topic-for` expression, then add `--select PROJECT=ARTICLE` for exactly
   one saved `status="selectable"` candidate per project. An `ambiguous`
   project also requires `--reason`. Stop here unless the published manifest
   reports `status="confirmed"`.
4. **Author `spec.json`.** Only after confirmation, turn the request into the
   frozen input contract: articles-first `series[]` with full fields per
   `references/CONTRACTS.md` (§1). Copy each confirmed
   `selection.article` verbatim as `series[].article`; the topic lives only in
   `request`.
5. **Validate.** Run the spec through the shared validator (aggregated model-
   readable stderr, exit 2 on violations). Example spec:
   `tests/fixtures/spec.example.json`.
6. **Run the stages**:
   - fetch (`fetch_pageviews.py`) — data → `series.csv` with `.cache/`,
     UA fail-fast and ~1 req/s throttle;
   - analyze (`analyze_trends.py`) — growth/seasonality/anomalies/confidence
     → `metrics.json`;
   - charts (`make_charts.py`) — PNG charts;
   - report (`build_report.py`) — Markdown report;
   - **one-command `run_all.py` arrives in v0.7** — not implemented yet; until
     then run the landed stages individually.
7. **Answer from `metrics.json` only.** Copy `confidence`, `confidence_reasons`,
   `as_of` and growth numbers verbatim; cite the charts.

### Resolver examples

Discovery:

```bash
python scripts/resolve_articles.py \
  --topic "intermittent fasting" \
  --projects en.wikipedia --projects pl.wikipedia \
  --topic-for pl.wikipedia="Post przerywany" \
  --out out/resolved.json --ttl-hours 24
```

Offline confirmation:

```bash
python scripts/resolve_articles.py \
  --topic "intermittent fasting" \
  --projects en.wikipedia --projects pl.wikipedia \
  --topic-for pl.wikipedia="Post przerywany" \
  --out out/resolved.json \
  --select en.wikipedia=Intermittent_fasting \
  --select pl.wikipedia=Post_przerywany
```

Resolver flags: required `--topic`; ordered `--projects`; repeatable
`--topic-for` and `--select`; `--out` (default `out/resolved.json`);
`--reason`; `--ttl-hours`; and `--verbose`. `--ttl-hours` is a non-negative
finite float whose default is a valid `WTI_TTL_HOURS` or `24.0`; an explicit
CLI value wins and `0` forces refetch. `--verbose` is a logging-only diagnostic
flag: it adds no field, changes no selection, and changes no API behavior.

## Policies

- **User-Agent:** set `WTI_USER_AGENT` to a descriptive value before any
  network call (Wikimedia 403s without it). Placeholder UAs fail fast with an
  actionable error — a non-descriptive identity never leaks.
- **Cache:** `WTI_CACHE` (default `.cache/`); resolver `--ttl-hours` defaults to
  a valid `WTI_TTL_HOURS` or `24.0`, explicit CLI wins, and `0` forces refetch.
  Only validated HTTP 200 endpoint payloads are cached.
- **Throttle:** ~1 request/second; resolver requests are serial; on 429 respect
  `Retry-After`; HTTP 403 is immediately fatal.
- **Confirmation:** the second resolver run is offline and candidate-bounded.
  Never edit `resolved.json` by hand, never select outside the saved candidate
  set, and never replace discovery evidence with an invalid confirmation.
- **Concurrency:** manifest writes are atomic, but two independent processes
  using the same `--out` must be serialized by the caller; otherwise the last
  completed atomic replace wins.
- **No edits:** the skill only reads public statistics and search metadata
  (`WP:BOT` / bot policy does not apply — there is no write).
- **Contracts:** never emit or read fields absent from `references/CONTRACTS.md`
  — pipeline stages must not break the frozen v0.1 contract, and
  `resolved.json` is a separate pre-stage contract (§6), not a change to
  `spec.json` or `metrics.json`.

## Red flags (stop and warn the user)

- `resolved.json` is not `status="confirmed"`, has any unresolved/partial/error
  project, or a selection is not one exact saved selectable candidate → do not
  run fetch or author `spec.json`.
- A changed or omitted `--topic`/`--projects`/`--topic-for` during confirmation
  → exit 2; the prior discovery manifest is intentionally preserved.
- AQS 404 is `unavailable` with a null total, never numeric zero; totals below
  1000 stay selectable as explicit low-volume hypotheses.
- Empty series or spec contract violations → exit 2 with model-readable
  stderr; fix the spec, do not rewrite code.
- `pct: null` in `metrics.json` means *not computable* — never read it as 0
  growth, and never report it as a number (ANAL-06).
- < 1000 views/month — noise, not signal.
- Comparison crossing 2015-05-01 — methodology change (spider/bot filtering).
- One giant spike driving the whole trend — check `anomalies` in `metrics.json`.

## Verification checklist

- [ ] `resolved.json` reached `status="confirmed"` with one selection per
      requested project before fetch/spec authoring.
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