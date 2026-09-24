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
| 0 | ok | read `metrics.json` |
| 2 | spec problem (validation) | fix the spec; model-readable stderr lists all violations |
| other | step crash | read stderr and report; do not rewrite pipeline code |

## 5. Golden fixtures

- `tests/fixtures/spec.example.json` — the executable frozen `spec.json`
  (valid fixture, plan 01; pinned series ids `pl-post-przerywany` /
  `cs-pust-prerusovany`).
- `tests/fixtures/metrics.example.json` — the golden `metrics.json` mirror
  (plan 02): 2 series, one demonstrating `pct: null` with a reason, clean
  variants per window.
- `tests/test_contracts.py` — enforcement: null-never-0-with-reason, finite
  numbers, enums, ranges, and the no-rollups rule.

Pipeline stages must not emit fields absent from this document — the enclosing
document is the D-04 contract freeze that later phases build against
(Phase 3 `analyze_trends.py` emits into this exact schema; Phase 6
`build_report.py` reads from it).