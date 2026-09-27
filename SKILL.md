---
name: wikipedia-trend-agent
description: "Analyzes Wikipedia pageviews trends for topics and language editions via the Wikimedia Pageviews API: compares interest across series, measures growth (3M/1Y/2Y with spike-excluded variants), detects seasonality and anomalies, and produces a metrics.json single source of truth plus a Markdown report with PNG charts (and, on request, an A4 PDF of the same numbers). Use when the user asks whether interest in a topic or language is growing («чи зростає інтерес до теми X»), wants a comparison across languages/topics, or needs to know how much a trend can be trusted. Runs on cheap/free models — stdlib-thin, one runtime dependency."
license: Apache-2.0
compatibility: "Python >= 3.11; HTTPS access to wikimedia.org"
metadata:
  version: 0.2.0
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

## Where the detail lives

Load one file per question. This body deliberately does not restate them.

| Question | File |
|---|---|
| What fields exist, and what does `pct: null` mean? | `references/CONTRACTS.md` (§1 spec, §2 metrics, §6 resolved, §7 charts, §8 report) |
| How do I read `confidence`, `confidence_reasons` and `trend_direction`? What may I say out loud? | `references/INTERPRETATION.md` |
| What are the UA, throttle, retry, 404 and cache rules? | `references/API_ACCESS.md` |
| What makes these numbers easy to misread? | `references/DATA_CAVEATS.md` |
| What does a conforming spec look like? | `assets/example.intermittent-fasting.json` |
| Which `--projects` codes exist, and where did that list come from? | `assets/wikipedia-projects.json` (`wikipedia-projects.v1`, the catalog `resolve_articles.py` validates against) |

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
   reports `status="confirmed"`. The second run is offline and
   candidate-bounded: it may only choose among candidates the first run saved.
4. **Author `spec.json`.** Only after confirmation, turn the request into the
   frozen input contract: articles-first `series[]` with full fields per
   `references/CONTRACTS.md` (§1). Copy each confirmed `selection.article`
   verbatim as `series[].article`; the topic lives only in `request`.
5. **Validate.** The first stage validates the spec and refuses every violation
   at once (aggregated model-readable stderr, exit 2). A conforming example is
   `assets/example.intermittent-fasting.json`.
6. **Run the pipeline — one command, normally:**

   ```bash
   python scripts/run_all.py --spec out/spec.json --out out
   ```

   | Exit | Meaning | What the model does |
   |---|---|---|
   | 0 | all four stages succeeded | read `out/metrics.json`, `out/report.md`, `out/charts.json` |
   | 2 | the spec is invalid | fix the spec against `references/CONTRACTS.md` §1 and re-run; stderr lists every violation at once |
   | 1 | fatal — empty series, no fetchable chunks, an unreadable input, a refused report language, or a missing User-Agent | read stderr, fix the article slug, the window, the `quality` flag or `WTI_USER_AGENT` |
   | 3 | partial — at least one series failed to fetch and at least one succeeded | read the per-series `series_id: message` stderr lines, fix that slug, re-run |
   | anything else | a crash | report it verbatim; do not rewrite pipeline logic |

   The four stages run in this order: fetch (`fetch_pageviews.py`) → analyze
   (`analyze_trends.py`) → charts (`make_charts.py`) → report
   (`build_report.py`). The orchestrator stops at the first failing stage and
   names it on stderr (`run_all: stage <name> failed: …`), so no later stage
   ever runs and no later artifact is written. `resolve_articles.py` is
   **not** part of it: it is the human-confirmed pre-stage in steps 1–3, and
   `run_all.py` runs only after `spec.json` exists. The command takes
   `--spec`, `--out`, `--verbose` and `--pdf`; it passes no `--log-scale`, so
   the one command always produces the linear chart regime, and the opt-in log
   regime is a deliberate second run of the chart stage.

   **A fifth stage, `--pdf`, is opt-in and OFF by default.** With the flag, a
   `pdf` stage runs after `report` and publishes `out/report.pdf` beside
   `report.md`, appending one object to the manifest's `formats[]`
   (`references/CONTRACTS.md` §8.1.1). It shows **the same numbers and the same
   localized strings** as the Markdown — the 11-column metrics table becomes one
   card per series, because eleven columns do not fit A4 — and it adds no
   runtime dependency: `requirements.txt` is still one line. It needs two
   optional things, and refuses with the exact command when either is missing:

   ```bash
   pip install -r requirements-pdf.txt                            # pypandoc
   winget install Typst.Typst --silent --accept-package-agreements  # the engine
   ```

   Without `--pdf` the pipeline is **byte-for-byte unchanged** — no new file,
   no new line on stdout, the same exit code. A PDF failure is a real failure
   and fails the pipeline; it is never downgraded to a warning.

   **The diagnostic path — the four stages individually.** Use this when one
   stage must be re-run with different options, or when you need its own
   output on stderr. Each stage takes the same `--spec` and `--out`:

   ```bash
   python scripts/fetch_pageviews.py --spec out/spec.json --out out   # -> series.csv
   python scripts/analyze_trends.py --spec out/spec.json --out out   # -> metrics.json
   python scripts/make_charts.py  --spec out/spec.json --out out      # -> PNG charts + charts.json
   python scripts/build_report.py --spec out/spec.json --out out     # -> report.md + report.manifest.json
   python scripts/build_pdf.py    --spec out/spec.json --out out     # -> report.pdf (optional 5th)
   ```

   `make_charts.py` also accepts `--log-scale`; `run_all.py` deliberately does
   not forward it. `build_pdf.py` is called directly exactly as `--log-scale`
   is — the two are the same kind of thing: a capability of one stage, invoked
   on its own or switched on by the orchestrator.
7. **Answer from `metrics.json` only.** Copy `confidence`, `confidence_reasons`,
   `as_of` and growth numbers verbatim; cite the charts. Read
   `references/INTERPRETATION.md` before writing a sentence about a trend.

### Resolver examples

Discovery:

```bash
python scripts/resolve_articles.py \
  --topic "intermittent fasting" \
  --projects en.wikipedia --projects pl.wikipedia \
  --topic-for pl.wikipedia="głodówka" \
  --out out/resolved.json --ttl-hours 24
```

Offline confirmation:

```bash
python scripts/resolve_articles.py \
  --topic "intermittent fasting" \
  --projects en.wikipedia --projects pl.wikipedia \
  --topic-for pl.wikipedia="głodówka" \
  --out out/resolved.json \
  --select en.wikipedia=Intermittent_fasting \
  --select pl.wikipedia=G%C5%82od%C3%B3wka_lecznicza
```

`--select PROJECT=ARTICLE` is **candidate-bounded**: the article must be one the
saved discovery run offered, or confirmation is refused. That is why the
`--topic-for` query and the `--select` value above are a pair — a slug you
remember (`Post_przerywany`) may name no article at all, and selecting it would
be rejected rather than silently creating one. Note also that pl.wikipedia has
no dedicated intermittent-fasting article: `Głodówka lecznicza` is fasting in
general, so a spec built from it must say in its `assumptions` that the two
series are not the same topic. Candidates carry the article **percent-encoded**
(`quote(title, safe="")`), and that encoded form is what goes verbatim into
`spec.series[].article`.

Resolver flags: required `--topic`; ordered `--projects`; repeatable
`--topic-for` and `--select`; `--out` (default `out/resolved.json`);
`--reason`; `--ttl-hours`; and `--verbose`. `--ttl-hours` is a non-negative
finite float whose default is a valid `WTI_TTL_HOURS` or `24.0`; an explicit
CLI value wins and `0` forces refetch. `--verbose` is a logging-only diagnostic
flag: it adds no field, changes no selection, and changes no API behavior.

## Policies

- **User-Agent:** set `WTI_USER_AGENT` to a descriptive value before any
  network call (Wikimedia 403s without it). Placeholder UAs fail fast with an
  actionable error — a non-descriptive identity never leaks. Full policy in
  `references/API_ACCESS.md`.
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
- **Numbers:** quote the pipeline's numbers, never recompute or transform one.
  The same value must never be derived a second time downstream, in a chart or
  in prose (ANAL-06 / `references/CONTRACTS.md` §7.3).

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
- **Exit 2 means fix the spec, not rewrite code.** It comes only from spec
  validation, and stderr lists every violation at once.
- **A chart stage that succeeds and a report stage that then refuses is a
  language-coverage limitation, not a broken run.** The chart stage accepts
  more languages than the report stage does (`references/CONTRACTS.md` §8.3
  names the `ja` case: a valid `charts.json`, then exit 1 and nothing written).
  Read the stderr line naming the language before concluding anything.
- **A `low` confidence reading is a hypothesis.** The wording rules in
  `references/INTERPRETATION.md` are binding on the answer, not advisory.

## Verification checklist

- [ ] `resolved.json` reached `status="confirmed"` with one selection per
      requested project before fetch/spec authoring.
- [ ] `run_all.py` exited 0, or its exit code was read against the table above
      and acted on (`2` → fix the spec; anything else → report verbatim).
- [ ] `metrics.json` exists and every number in the answer matches it.
- [ ] `as_of` stated (last complete day of data).
- [ ] `confidence` + `confidence_reasons` copied verbatim.
- [ ] `trend_direction` reported as the value it is — never as "interest is
      rising"; `inconclusive` and `noise` reported as answers, not failures.
- [ ] Every growth figure is the `clean` variant, quoted with its volume base.
- [ ] Assumptions and limitations listed; `low` confidence phrased as a
      hypothesis, never a conclusion.
- [ ] Contracts hold and tests are green:
      `cd wikipedia-trend-agent && python -m pytest -q`.

## Performance on cheap model

- One input file (`spec.json`), one command, one output file to read
  (`metrics.json`).
- All arithmetic lives in code — the model quotes numbers, never recomputes.
- Stdlib-first: the only runtime dependency is `matplotlib>=3.11` (charts).
- One file per question, so a cheap model loads `SKILL.md` plus at most one
  reference file instead of the whole contract.
- If a stage crashes: read exit code + stderr, fix the spec, do not rewrite
  pipeline logic.
