# Contracts — spec.json & metrics.json (frozen, v0.1)

This file is the single source of truth for the frozen I/O contracts of the
`wikipedia-trend-agent` skill (ANAL-06). Every pipeline stage emits into and
reads from exactly the fields documented here — nothing more, nothing less.

The contract language is English; Ukrainian strings appear only as example
values where a field carries human-readable text.

## Versioning

**Contract v0.1 — frozen at Phase 1 (2026-09-23).**
Any change requires a contract-bump review; pipeline stages must not emit
fields absent from this document.

## 1. `spec.json` — the input contract (authored by the model)

The model converts a natural-language request into exactly one `spec.json`.
Validation is strict and aggregated (D-07): all violations are collected into
one stderr message with field/value/why/how-to-fix; a violating spec exits
with code 2 (D-08).

### Root fields

| Field | Required | Type | Description |
|---|---|---|---|
| `name` | yes | string | Research identifier; copied to `metrics.json` as `spec_name` |
| `request` | yes | string | The user's request (free text) |
| `language` | yes | string | Report language, e.g. `"uk"` (D-02) |
| `window` | yes | object | Shared analysis window — identical for every series (D-02) |
| `series` | yes | array of objects | Non-empty; the exact articles to compare (D-01) |
| `assumptions` | no | array of strings | Assumptions told to the user before analysis |
| `quality` | no | object | Quality gates, e.g. `{"fail_on_empty_series": true}` |

### `window` fields

| Field | Required | Description |
|---|---|---|
| `start` | yes | `YYYYMMDD` start date (Pageviews API format, e.g. `20240923`) |
| `end` | yes | `YYYYMMDD` end date (e.g. `20260920`) |
| `granularity` | no | `"daily"` only in v1 |

### `series[]` item fields

| Field | Required | Description |
|---|---|---|
| `id` | yes | Stable series id; becomes `series_id` in `metrics.json` (see §3) |
| `project` | yes | Project code, e.g. `pl.wikipedia`, `uk.wikipedia`, `cs.wikipedia` |
| `article` | yes | Article slug with underscores, no project prefix (e.g. `Post_przerywany`) |
| `label` | yes | Human-readable label for charts/report, e.g. `"Польська: інтервальне голодування"` |
| `language` | yes | Language code, e.g. `"pl"`, `"cs"` |

### Project codes are stems, not hosts

A project code is a Wikimedia Site Matrix **host stem** (`en.wikipedia`), not a
hostname. The two endpoints use it differently, so never reuse a project code as
a host:

| Endpoint | How the project code appears | Example |
|---|---|---|
| Action API (`resolve_articles.py`) | stem + `.org` as the **host** | `https://en.wikipedia.org/w/api.php` |
| AQS Pageviews (`fetch_pageviews.py`) | bare stem as a **path segment** | `https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/…` |

Both forms come from the committed `assets/wikipedia-projects.json`
(`wikipedia-projects.v1`, 364 codes) and are validated by exact membership before
any URL is built.

### Unknown-field rejection

Unknown fields at any level are rejected (validation error), not ignored: the
frozen contract is the only allowed surface (D-01/D-02/D-07). Exit codes are
in §4.

## 2. `metrics.json` — the metrics single source of truth (SSoT, ANAL-06)

Every number the pipeline publishes (analysis → charts → report → answer)
comes from exactly this file. No downstream component recomputes values that
already exist here. `tests/test_contracts.py` fails on any drift from this
document.

### Top level

| Field | Required | Type | Description |
|---|---|---|---|
| `spec_name` | yes | string | The `name` of the spec the metrics were computed from |
| `as_of` | yes | string | `YYYY-MM-DD` — date of the last complete day of data |
| `generated_from` | yes | string | Path of the spec that produced this file |
| `series` | yes | array | Non-empty; one item per spec series, same order |

**No cross-series rollups exist (D-03).** `metrics.json` is strictly per-series
plus the three identity fields above. There are no cross-series totals, no
top-series rankings, no overall/across-series growth, and no aggregate keys —
anywhere in the document.

### Per-series fields

| Field | Required | Type | Description |
|---|---|---|---|
| `series_id` | yes | string | Mirrors the spec series item `id` (see §3) |
| `project` | yes | string | Project code, e.g. `pl.wikipedia` |
| `article` | yes | string | Article slug as in the spec |
| `label` | yes | string | Human-readable label, e.g. `"Польська: інтервальне голодування"` |
| `language` | yes | string | Language code |
| `total_views` | yes | integer | Sum of daily views over the window |
| `avg_daily_views` | yes | number | `total_views / period.days`, 1 decimal |
| `period` | yes | object | `{start: YYYY-MM-DD, end: YYYY-MM-DD, days: integer}` (inclusive) |
| `growth` | yes | object | `{m3, y1, y2}` — equal-length growth windows (§2.1) |
| `anomaly_share` | yes | number | Share of anomalous days, ∈ [0, 1] |
| `trend_direction` | yes | string | `up` \| `down` \| `flat` \| `noise` \| `inconclusive` |
| `confidence` | yes | string | `high` \| `medium` \| `low` |
| `confidence_reasons` | yes | array of strings | Rubric reasons, e.g. `"період ≥ 2 роки"`, `"обсяг ≥ 1000 переглядів на місяць"` |
| `seasonality` | yes | object | `{months: [integer 1–12], note: string}` — same-month YoY control |
| `anomalies` | yes | array of objects | Each `{date: YYYY-MM-DD, value: number, median: number}` |

### 2.1 Growth windows (`growth.m3`, `growth.y1`, `growth.y2`)

Each window is an equal-length slice ending at the last complete data day
(91 / 365 / 730 days).

| Field | Type | Description |
|---|---|---|
| `pct` | finite number \| null | Relative change vs the preceding equal-length window, `round(x, 1)`. **`pct: null` means NOT COMPUTABLE — never 0; a null `pct` must carry a `reason`.** |
| `abs` | integer \| null | Absolute change (same units as `avg_daily_views`); `null` when the window cannot be computed |
| `start` | string | Window start `YYYY-MM-DD` |
| `end` | string | Window end `YYYY-MM-DD` |
| `clean` | object | Spike-excluded variant: `{pct: finite number\|null, abs: integer\|null}` — same null semantics |
| `reason` | string (only when `pct` is null) | Model-readable explanation, e.g. `"недостатньо даних для двох вікон"` |

**The null rule (ANAL-06):** `pct: null` means *not computable*, never *zero*.
A literal `0` must never be used to mark a missing window — a model reading
`0` would interpret it as "no growth". A null `pct` always carries a non-empty
`reason` right beside it (in the same window object; for the `clean` variant —
inside the `clean` object).

### 2015-05-01 methodology crossing

Windows whose comparison crosses 2015-05-01 (spider/bot filtering change) are
flagged in `confidence_reasons` and confidence is penalized. For a 2024–2026
comparison window the crossing does not apply.

## 3. `spec.json` `id` → `metrics.json` `series_id`

Each `spec.json` series item carries `id` (e.g. `pl-post-przerywany`). The
analysis stage copies it verbatim into `metrics.json` as `series_id` — same
value, same order as `spec.series[]`. Consumers must never invent, rename, or
reorder these ids.

## 4. Exit codes

| Code | Meaning | Agent action |
|---|---|---|
| 0 | all series ok | read `series.csv` (and downstream `metrics.json`) |
| 2 | spec problem (validation) | fix the spec; model-readable stderr lists all violations |
| 1 | fatal: `fail_on_empty_series` or no fetchable chunks | read stderr; fix article slugs, window, or quality flag |
| 3 | partial: at least one series failed and at least one succeeded | read `<series_id>: <message>` stderr lines and retry |

### 4.1 `series.csv` (fetch output)

`fetch_pageviews.py --out DIR` writes `DIR/series.csv` with the exact header
`date,views,series_id,project,article`. Dates use `YYYY-MM-DD`; `views` is an
integer; rows are sorted by `(series_id, date)`.

Rows represent `(series, present-day)` observations from the 200 response. A
day omitted by the AQS response is absent from the CSV and is never imputed,
estimated, or zero-filled. A zero appears only from confirmed no-views 404
classification for the requested chunk.

## 5. Golden fixtures

- `tests/fixtures/spec.example.json` — the executable frozen `spec.json`
  (valid fixture, plan 01; pinned series ids `pl-post-przerywany` /
  `cs-pust-prerusovany`).
- `tests/fixtures/pageviews.200.json` — captured AQS 200 `items` response for
  response-shape and cache-envelope tests.
- `tests/fixtures/pageviews.404.json` — captured AQS 404 response for network-free
  404 taxonomy tests.
- `tests/fixtures/pageviews.403.txt` — captured missing-User-Agent policy response
  for 403 fail-fast tests.
- `tests/fixtures/spec.live.example.json` — probe-valid live smoke spec for
  `Warszawa` and `Albert_Einstein`.
- `tests/fixtures/metrics.example.json` — the golden `metrics.json` mirror
  (plan 02): 2 series, one demonstrating `pct: null` with a reason, clean
  variants per window.
- `tests/test_contracts.py` — enforcement: null-never-0-with-reason, finite
  numbers, enums, ranges, and the no-rollups rule.

Pipeline stages must not emit fields absent from this document — the enclosing
document is the D-04 contract freeze that later phases build against
(Phase 3 `analyze_trends.py` emits into this exact schema; Phase 6
`build_report.py` reads from it).

## 6. `resolved.json` — `resolved.v1` (pre-stage, Phase 4)

`resolved.json` is a **separate pre-stage contract**. It does not change any
frozen `spec.json` or `metrics.json` field in §1–§4: the model authors
`spec.json` only after `resolved.json` reaches `status="confirmed"`, and the
confirmed `selection.article` becomes the exact `spec.series[].article` value.

### 6.1 Top-level fields

| Field | Type | Description |
|---|---|---|
| `contract_version` | `"resolved.v1"` | Exact version; anything else is rejected. |
| `run_mode` | `"discover"` \| `"confirm"` | Network discovery or offline confirmation. |
| `status` | enum below | Run-level outcome; drives the exit code. |
| `topic` | string | The one shared natural-language topic (`--topic`). |
| `topic_overrides` | array of `{project, query}` | Ordered `--topic-for` expressions exactly as supplied. |
| `generated_at` | timezone-aware ISO-8601 string | Discovery publication time. |
| `volume_window` | object | `{start, end, days: 30, access: "all-access", agent: "user", granularity: "daily"}`; one shared window of 30 complete UTC days ending yesterday. |
| `projects` | ordered array | Exactly the ordered `--projects` context, one record each. |

Enums: top `status` is `awaiting_confirmation | confirmed | unresolved |
partial_error | error`; project `status` is `ready | ambiguous | unresolved |
error`; candidate `status` is `selectable | unresolved`; volume `status` is
`available | unavailable`; `query_source` is `shared | project_override`.

### 6.2 Project record

Each project record carries exactly `project`, `effective_query`,
`query_source`, `status`, `recommendation`, ordered `search_hits`, `candidates`,
`selection`, `reason`, and `error`.

- **ordered `search_hits`** is a list of `{title, rank}` in API order with
  `rank == position + 1`; it is `null` only when an error occurred before a
  validated search list existed. A metadata/AQS error after a completed search
  still keeps the ordered list.
- `ready` has exactly one selectable candidate and a non-null recommendation;
  `ambiguous` has multiple selectable candidates, `recommendation=null`, and
  requires `--reason` on confirmation.
- **D-11 zero hits** is exactly `reason="search_no_hits"`, `search_hits=[]`,
  and `candidates=[]`.
- **D-08 structural unresolved** is exactly `reason="no_selectable_candidates"`
  with a non-empty `candidates` list whose records retain `status="unresolved"`,
  `article=null`, redirect provenance, and a stable reason: `redirect_cycle`,
  `redirect_too_deep`, `missing_target`, `non_article_namespace`, or
  `disambiguation_page`. Flattening or deleting that provenance invalidates the
  document.
- **Any unresolved project takes precedence over `partial_error`**, and an
  all-`error` run is `error`.

### 6.3 Candidate, volume, and selection

A selectable candidate carries exactly `status`, `article`, `title`,
`namespace` (`0`), `input_titles`, `redirect_chain`, `search_rank`,
`exact_title_match`, `disambiguation` (`false`), `volume`, and `reason`
(`null`). An unresolved candidate carries the same provenance fields without
`volume`, plus its stable `reason`.

Volume is one shared 30-day AQS evidence object. Every item must match the
expected project, the **encoded canonical article**, `all-access`, `user`,
`daily`, the requested window, and non-negative integer views **before** any
sum, cache write, or `candidate.volume` mutation. A valid 200 is `available`
with the exact sum, observed days, and `low_volume=(total_views < 1000)`.
AQS 404 is `unavailable` with `total_views=null`, `low_volume=null`, and reason
`aqs_404_zero_or_not_loaded` — never numeric zero. Low-volume candidates remain
selectable.

`selection` is `null` in discovery. A valid confirmation sets it to exactly
`{article, title, reason, source:"model_confirmation"}` for **exactly one
selectable candidate per requested project**; the selection is candidate-bounded
and the `article` is copied verbatim into `spec.series[].article`.

### 6.4 Cache, retry, and resource contract

Every search, metadata, and AQS request is cache-first. The JSON cache key is
SHA-256 of the full URL; the envelope is exactly
`{fetched_at, status: 200, response}`. An endpoint validator — including the
expected AQS series identity — must pass before a cached value is trusted or a
new body is written. HTTP-200 Action `error`/non-empty `errors` bodies, 403,
exhausted 429/5xx, malformed bodies, and oversized bodies never create or trust
a cache entry and never mutate candidate state.

`--ttl-hours` is a non-negative finite float. Its parser default is a valid
`WTI_TTL_HOURS` value when present, otherwise `24.0`; an explicit CLI value
wins; `0` bypasses cache reads for the run while retaining the same validated
write envelope and full-URL key. HTTP 403 is immediately fatal; 429, 5xx, and
transport failures use at most three attempts and honor integer or HTTP-date
`Retry-After` with a 5.0-second fallback.

`MAX_PROJECTS=8` rejects over-fanout before `user_agent()`, host construction, or
transport. `MAX_RESPONSE_BYTES=1_048_576` rejects a declared over-limit
`Content-Length` before reading and otherwise performs exactly
`read(MAX_RESPONSE_BYTES + 1)`; the bounded resolver transport raises
`ResponseTooLarge` without an unbounded read.

### 6.5 Confirmation, atomicity, and exit codes

Confirmation is offline and candidate-bounded. It repeats `--topic`, the ordered
`--projects`, and every `--topic-for` expression: an exact replay is accepted;
a changed or omitted expression is rejected. The complete saved document is
validated before any in-memory record changes, the confirmed document is
validated again in `confirm` mode, and only then is it published with one
atomic `os.replace`. Any invalid transition returns 2 with the prior discovery
bytes unchanged; a publication failure returns 1, reports the stable
`publication_error` code, and leaves no `.resolved.json.*.tmp` staging file.

| Situation | Manifest | Exit |
|---|---|---|
| input preflight / invalid confirmation | prior bytes preserved | 2 |
| all requested projects ready or ambiguous | `awaiting_confirmation` | 0 |
| any unresolved project (including mixed with errors) | `unresolved` | 2 |
| some error with a successful sibling | `partial_error` | 3 |
| every project failed, or fatal 403/UA/runtime | `error` / none | 1 |
| valid exactly-one-per-project confirmation | `confirmed` | 0 |

`--verbose` is an optional logging-only diagnostic flag: it adds no manifest
field, does not alter selection, and changes no API behavior.

**Concurrency assumption:** atomic replacement prevents partial files and
requests are serial within one process, but two independent processes targeting
the same `--out` are not serialized by the resolver. They are
**caller-serialized**: the caller must serialize them. Without that caller
serialization, the **last completed atomic replace wins**.

### 6.6 `resolved.v1` CLI contract

```bash
# run 1 — discovery
python scripts/resolve_articles.py --topic "intermittent fasting" \
  --projects en.wikipedia --projects pl.wikipedia \
  --topic-for pl.wikipedia="Post przerywany" \
  --out out/resolved.json --ttl-hours 24

# run 2 — offline confirmation
python scripts/resolve_articles.py --topic "intermittent fasting" \
  --projects en.wikipedia --projects pl.wikipedia \
  --topic-for pl.wikipedia="Post przerywany" \
  --out out/resolved.json \
  --select en.wikipedia=Intermittent_fasting \
  --select pl.wikipedia=Post_przerywany
```

Flags: required `--topic`; ordered `--projects`; repeatable `--topic-for
PROJECT=QUERY` and `--select PROJECT=ARTICLE`; `--out` (default
`out/resolved.json`); `--reason`; `--ttl-hours`; and logging-only `--verbose`.

### 6.7 Resolver fixtures

- `tests/fixtures/resolve.search.*.json` — committed Action search responses.
- `tests/fixtures/resolve.redirects.*.json` — redirect/pageprops metadata.
- `tests/fixtures/resolve.api-error.*.json` — HTTP-200 Action error envelopes.
- `tests/fixtures/pageviews.200.json` / `pageviews.404.json` — AQS volume
  states used by the resolver evidence contract.
- `assets/wikipedia-projects.json` — the versioned offline project allowlist.

No test may reach the network; every transport is injected or forbidden.
