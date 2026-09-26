# Contracts — spec.json, metrics.json, resolved.v1, charts.v1 & report.v1 (frozen, v0.1)

This file is the single source of truth for the frozen I/O contracts of the
`wikipedia-trend-agent` skill (ANAL-06). Every pipeline stage emits into and
reads from exactly the fields documented here — nothing more, nothing less.
Five contracts are frozen here: `spec.json` (§1), `metrics.json` (§2),
`resolved.v1` (§6), `charts.v1` (§7) and `report.v1` (§8).

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
- `tests/fixtures/metrics.anomalies.example.json` and
  `tests/fixtures/series.anomalies.example.csv` — the spike-injected pair: a
  non-empty `anomalies[]` and the 19.6-raw / 16.7-clean divergence make the
  mandatory anomaly overlay testable, and the overlay chart is the only
  artifact that carries it.
- `tests/fixtures/series.gaps.example.csv` — a per-series internal hole, so the
  `no data` band is testable without the `out/series.csv` sort-boundary
  artifact.
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

## 7. `charts.json` — `charts.v1` (chart stage, Phase 5)

`charts.json` is a **separate stage contract**. It changes no frozen
`spec.json`, `metrics.json` or `resolved.v1` field in §1–§6: the chart stage
**renders** `metrics.json` and `series.csv` and **never recomputes** a value
(ANAL-06). Every number and every label that reaches a chart is copied from a
field this document already froze; the one permitted derivation is named
explicitly in §7.3 and is a display curve, not a claim.

### 7.1 Top-level fields

| Field | Type | Description |
|---|---|---|
| `contract_version` | `"charts.v1"` | Exact version; anything else is rejected. |
| `spec_name` | string | The `name` of the spec the charts were built from, copied from `metrics.json`'s own `spec_name`. |
| `as_of` | string | `YYYY-MM-DD` — copied verbatim from `metrics.json`. |
| `language` | string | The validated spec's `language` (the DOCUMENT language, not one series' language). |
| `generated_from` | string | Path of the `metrics.json` this stage read. |
| `metrics_sha256` | 64-char lowercase hex | SHA-256 of the **exact `metrics.json` bytes the stage read**. |
| `charts` | ordered array | The chart inventory; one object per PNG (§7.2). |

`metrics_sha256` is an **integrity and staleness mechanism, not a security
control**. A consumer compares it against the digest of the `metrics.json` it
holds to decide whether the manifest still describes that file — which is how
staleness is detected **without comparing timestamps**, since an mtime carries
no information about *which* content was rendered. Nothing is authenticated by
it, and no later phase may build access control on it.

### 7.2 Per-chart fields

`kind` is exactly one of `timeseries`, `growth`, `overlay`.

| Field | Type | Description |
|---|---|---|
| `kind` | enum | The chart form. |
| `series_id` | string | The one series this chart shows; on the two per-series kinds only. |
| `series_ids` | ordered array of strings | The overlay's full membership, in `metrics.json` order; on the overlay only. |
| `spec_index` | integer | The series' position in `spec.series[]`; on the two per-series kinds only, so chart order is inspectable rather than merely implied by list position. |
| `label` | string | The series' own `metrics.json` label, byte-for-byte. |
| `language` | string | That series' `language` code. |
| `filename` | string | A **bare relative name** — no directory separator, no `..`, no drive letter (§7.2.1). |
| `yscale` | `"linear"` \| `"log"` | The regime this entry was drawn in. `"log"` only under the explicit `--log-scale` flag, and never on a `growth` entry. |
| `y_limits` | `[number, number]` | The **display bounds the renderer used** for the axis carrying the plotted number — a display fact, not a data claim. On the horizontal `growth` form that axis is x, and the field keeps its contract name so one rule covers all three kinds rather than a second field name appearing here. |
| `x_limits` | `[YYYY-MM-DD, YYYY-MM-DD]` | The date-axis domain, read from the series' own `metrics.json` `period` block. Absent on a chart with no date axis (§7.2.2). |
| `points` | integer | How many observations this chart drew. On the overlay it is the **sum over its lines**; on `growth` it is `0`, because a bar chart draws no daily series and does not borrow its sibling's count. |
| `gaps` | array of objects | Each `{series_id, start, end, days}` — a tagged absent-day range, `days` inclusive. Never empty-by-absence: written even when `[]`. |
| `anomalies_drawn` | integer | How many anomaly markers this chart drew. |
| `bars` | array of objects | `growth` only (§7.2.3). |
| `note` | string | A stable ASCII interface string (§7.2.4). |
| `subtitle` | string | The chart's own method disclosure, composed from the document language's tokens. |
| `log_masked_points` | integer | How many of this entry's own readings were non-positive and therefore not drawable on a log axis. Always written, and `0` on a linear entry. |
| `log_note` | string | The visible log disclosure; present **exactly** when `yscale` is `"log"`. |

**The overlay's per-series render data is not published.** The overlay declares
its membership through `series_ids` and through the per-series entries it
references; a nested `series_lines` would duplicate `metrics.json` inside the
manifest for no consumer. A test asserts no entry ever carries a `series_lines`
key, so the render-only boundary stays a boundary.

#### 7.2.0 The `2N+1` inventory rule

For **N** series the stage publishes exactly **`2N+1`** charts: one `timeseries`
and one `growth` per series, plus one `overlay` — which is **always** published,
including at N=1, because a one-series comparison view is still the view a
reader embeds as the headline. `charts[]` is ordered by `spec.series[]` order,
with the `timeseries` and `growth` entries of a series adjacent and the overlay
**last**. Each `filename` in the array names a PNG that exists in the output
directory, one for one, so a consumer can join the two without a lookup table.

#### 7.2.1 The deterministic filename rule

`filename` is `chart_<series_id>_<kind>.png` for the two per-series kinds, and
the fixed `chart_overlay.png` for the overlay — the overlay's name is not
derived from a `series_id`, which is also what keeps it from colliding with a
per-series name. A `series_id` that is not `[A-Za-z0-9._-]+` is **refused**
before a filename is built, so a chart name can never escape the output
directory. A consumer embeds a chart by joining this name onto the directory it
was given; no path traversal is possible from a conforming manifest.

#### 7.2.2 The per-entry key-set rule

The 18 names above are a **union across kinds**, not a per-entry key list. No
single entry carries all 18, and each name is partitioned three ways:

| Partition | Names | Guarantee |
|---|---|---|
| **Always emitted (11)** | `kind`, `label`, `language`, `filename`, `yscale`, `y_limits`, `points`, `gaps`, `anomalies_drawn`, `subtitle`, `log_masked_points` | Present on every entry, whatever the kind and whatever the flags. `gaps` is written even when it is `[]` and `log_masked_points` even when it is `0`, so a consumer asks "does this chart have gaps?" and "was anything masked?" without a key-absent special case. |
| **Owned by a kind or a mode (6)** | `series_id`, `spec_index`, `bars`, `series_ids`, `note`, `log_note` | `series_id`/`spec_index` on the two per-series kinds — the overlay belongs to no single `spec.series[]` position, so it publishes neither; `bars` on `growth`; `series_ids` and `note` on `overlay`; `log_note` exactly when `yscale` is `"log"`. |
| **Owned by a value (1)** | `x_limits` | Present when the entry has a date axis. In practice that is `timeseries` and `overlay` and not `growth`, but the **emitter's guard is `x_limits is not None`, not a kind test** — a future kind with a date axis would be published automatically, and this document states the observed invariant rather than a guarantee the code does not make. |

The same care applies to `note`: its emitter guard is `note is not None`, and
`note` is overlay-only **in practice**, not by a kind test.

The reason the sets partition is the **no-null rule**: a key whose value is
null, or is meaningless for that kind, is **omitted** rather than written as
`null`. A consumer therefore distinguishes "absent" from "zero" by reading the
key's own type, never by finding a `null` in the document. The same rule holds
inside a `bars[]` object: `pct`, `abs` and `reason` are omitted when the clean
percentage is not computable, and the explicit `bar_null` boolean is how a
consumer reads "not computable" from a field rather than from an absence.

#### 7.2.3 The `bars[]` object (7 names, same freeze)

| Field | Type | Description |
|---|---|---|
| `window` | string | The growth window: `m3`, `y1` or `y2`. |
| `label` | string | The window's human-readable name, e.g. `"1 year"`. |
| `pct` | finite number | The **clean** relative change — `growth.<window>.clean.pct`, never the raw `pct` (§7.3). Omitted when not computable. |
| `abs` | integer | The clean absolute change, same units as `avg_daily_views`. Omitted when not computable. |
| `base_avg_daily_views` | number | The volume base the contract already froze: `metrics.json`'s own `avg_daily_views` (§7.3). |
| `reason` | string | Present exactly when `pct` is absent; the contract's own `reason` for the null window (§2.1). |
| `bar_null` | boolean | Always written. `true` marks the explicit "hatched n/a" bar. |

#### 7.2.4 The manifest `note` and the on-image disclosure are not the same string

The manifest `note` is a **stable ASCII interface string** — the same constant
regardless of the spec's language, because a machine quotes it verbatim. The
equivalent disclosure drawn **on the image** is the localized token for the
document's language, because a reader needs it in their own words. A consumer
must not assume the two strings are equal, and a test asserts the asymmetry.

### 7.3 The never-recompute rule

This is the load-bearing clause of `charts.v1`. **Every plotted and labelled
value is copied from `metrics.json` or `series.csv`.**

- A growth bar carries `growth.<window>.clean.pct` and `clean.abs` — never the
  raw `pct`/`abs`, and never a percentage recomputed from the CSV. The 19.6-raw
  / 16.7-clean divergence in the committed anomalies fixture is exactly the
  difference a recomputation would erase.
- The volume base is the contract's `avg_daily_views`, never a mean
  recomputed from `series.csv`.
- The anomaly overlay uses each `anomalies[]` entry's own `median` and `value`
  as the segment endpoints. Nothing rescales, narrows or re-derives them.
- The date domain is the series' own `period` block, published as `x_limits`.
- The **single permitted derivation** is the centered **7-day rolling median**
  over the plotted series, which exists to draw `series.csv`. It is a display
  curve: it may never feed a numeric label or any manifest value.
- A `views=0` row in `series.csv` is **data** and is drawn. An absent calendar
  day is expressed only as a line break plus a `gaps` record; it is never
  imputed, zero-filled or bridged.

A consumer inherits this rule. A number the manifest already carries must never
be derived a second time downstream.

### 7.4 Axis, scale, and null rendering policy

- The y-axis is anchored at **`0`** on the `timeseries` and `overlay` axes with
  no exception.
- Growth axes **never receive a zero floor**, because `clean.pct` may be
  negative and a zero floor clipped a measured −22.0% decline out of the picture
  entirely — a falling topic drawn as a small rising one. The bounds always
  *contain* 0.0, and an explicit zero reference line keeps a decline legible.
- A `null` growth renders as a **hatched grey bar marked `n/a`** carrying the
  window's contract `reason`. It is never omitted and never zero, and a
  `pct: 0.0` is a solid bar structurally distinct from it.
- A log axis exists **only** under the explicit `--log-scale` flag, applies only
  to the `timeseries` and `overlay` axes, sets `nonpositive="mask"` explicitly
  rather than inheriting matplotlib's default, and discloses
  `log_masked_points` **both in the manifest and as visible chart text**. A
  regime change the reader did not ask for is a different chart, not a better
  one, so no threshold in the data can select it.
- **Documented v1 gap:** the bundled DejaVu font covers Latin and Cyrillic. A
  non-Latin script such as CJK renders as tofu with a single stderr warning
  naming the language; there is no font fallback in v1.

### 7.5 Exit codes, atomicity, and publication

| Situation | Manifest | Exit |
|---|---|---|
| all charts rendered | `charts.v1` published | 0 |
| invalid spec | nothing written | 2 |
| missing/invalid `series.csv` or `metrics.json`, unsafe `series_id`, non-finite value, publication failure | prior bytes preserved, no staging file | 1 |

**There is no `3` (partial) case.** §4 defines 3 for a stage that completes some
units and fails others; this stage has no such outcome, because a
half-populated `charts.json` is worse than none at all — a reader who finds
four PNGs and a manifest naming five has been told something false about the
fifth. So a series that cannot be charted fails the whole stage: the manifest is
published only after **every** render has returned.

**Atomicity:** a same-directory staging file plus one `os.replace`. A
publication failure leaves the prior manifest bytes untouched and no
`.charts.json.*.tmp` behind.

**Concurrency assumption:** atomic replacement prevents partial files and
requests are serial within one process, but two independent processes targeting
the same `--out` are not serialized by the chart stage. They are
**caller-serialized**: the caller must serialize them. Without that caller
serialization, the **last completed atomic replace wins**.

### 7.6 `charts.v1` CLI contract

```bash
# run 1 — the normal run
python scripts/make_charts.py --spec out/spec.json --out out

# run 2 — the same data with the opt-in log regime
python scripts/make_charts.py --spec out/spec.json --out out --log-scale
```

Flags: `--spec` (required); `--out` (default `out`); `--log-scale` (opt-in log
y-axis for the `timeseries` and `overlay` charts only, with a counted masked-
points disclosure); and logging-only `--verbose`, which adds no manifest field
and changes no behavior.

### 7.7 Chart fixtures

- `tests/fixtures/series.example.csv` — the committed two-series daily input
  the golden `metrics.example.json` was derived from.
- `tests/fixtures/series.anomalies.example.csv` and
  `tests/fixtures/metrics.anomalies.example.json` — the spike-injected pair
  whose non-empty `anomalies[]` and 19.6-raw / 16.7-clean divergence make the
  mandatory overlay branch and the clean-growth copy testable.
- `tests/fixtures/series.gaps.example.csv` — a per-series internal hole, which
  makes the `no data` band testable without the `out/series.csv` sort-boundary
  artifact.

No test may reach the network; every transport is injected or forbidden.

## 8. `report.md` & `report.manifest.json` — `report.v1` (report stage, Phase 6)

`report.md` and `report.manifest.json` are a **separate stage contract**, and
the report stage is a **reader**. It reads `metrics.json`, `charts.json` and
`spec.json` and **never recomputes** a value (ANAL-06) — the rule `charts.v1`
§7.3 froze for the chart stage, and §7.3's own sentence closes the loop: *a
consumer inherits this rule*. It changes no frozen `§1`–`§7` field, and it opens
`series.csv` at no point: every number the document displays was already
published by an earlier stage, so there is nothing here for it to derive.

### 8.1 Top-level fields

| Field | Type | Description |
|---|---|---|
| `contract_version` | `"report.v1"` | Exact version; anything else is rejected. |
| `spec_name` | string | The `name` of the spec the report was written for, copied verbatim from `metrics.json`'s own `spec_name`. |
| `as_of` | string | `YYYY-MM-DD` — copied verbatim from `metrics.json`. |
| `language` | string | The validated spec's `language` (the DOCUMENT language, not one series' language). |
| `generated_from` | string | Path of the `metrics.json` this stage read. |
| `metrics_sha256` | 64-char lowercase hex | SHA-256 of the **exact `metrics.json` bytes the stage read**. |
| `charts_sha256` | 64-char lowercase hex | SHA-256 of the **exact `charts.json` bytes the stage read**. |
| `report_filename` | string | The bare relative name `report.md` — no directory component, so §8.5's join needs no path work. |
| `metrics_shown` | ordered array of strings | The metric paths the document rendered, grammar `series[i].field` and `series[i].growth.<window>.clean.pct` (§8.6). |
| `formats` | ordered array of objects | One `{format, filename}` object per published rendering. v1 writes exactly `[{"format": "markdown", "filename": "report.md"}]`. |

`formats` is an array of **objects**, not of bare strings, and that is the whole
point of the shape: a v1.x PDF entry is one appended object and one more file,
never a rename of the Markdown slot. `report_filename` is the bare relative name
for the same reason — a consumer resolves it against the directory it was given.

`metrics_sha256` and `charts_sha256` are an **integrity and staleness
mechanism, not a security control**. A consumer compares them against the
digests of the files it holds to decide whether the manifest still describes
that content — which is how staleness is detected **without comparing
timestamps**, since an mtime carries no information about *which* content was
rendered. Nothing is authenticated by them, and no later phase may build access
control on them. Both digests come from the **same one-pass read** that
validated each document: a second read could read different bytes than the one
that was validated, and the digest would then attest to content the report never
saw.

**Recorded deviation from ROADMAP SC#4's wording.** That criterion reads
"when `metrics.json` is newer than the report". This contract detects staleness
by **digest comparison, never by mtime**, and the reason is that an mtime
comparison yields a false negative whenever a file is copied, checked out, or
restored with new timestamps and identical content — the report would then be
declared stale while describing exactly the right bytes. The rationale is
§7.1's own, applied to the sibling manifest: an mtime carries no information
about which content was rendered.

### 8.2 The fixed section order

The document carries exactly six sections, in this order and no other:

| # | Heading (`uk`) | What it carries |
|---|---|---|
| 1 | `Висновок` | The per-series conclusion, each with its own window, percentage and volume base. |
| 2 | `Метрики` | The metrics table, carrying `as_of` and the growth windows. |
| 3 | `Наскільки можна довіряти` | The confidence level plus its reasons. |
| 4 | `Графіки` | The `2N+1` image inventory (§8.5). |
| 5 | `Обмеження та припущення` | The assumptions, notes and counts that bound the reading. |
| 6 | `Наступний крок` | A fixed non-numeric localized token. |

The order is **machine-checkable**: a test asserts each heading's index in the
rendered document and requires them strictly increasing, not merely present. A
set comparison would pass on a document whose sections were in the wrong order,
which is the failure this clause exists to make impossible.

The four headings `Висновок`, `Наскільки можна довіряти`,
`Обмеження та припущення` and `Наступний крок` are the **exact strings** the
roadmap's first success criterion names, character for character. The
criterion describes two further sections without naming them; `Метрики` and
`Графіки` are **this document's own naming** for those two. A developer who
wants different wording changes the success criterion, not this table alone.

### 8.3 The report's own words

Two tables carry every word the document writes, and both are **fail-closed**:

- `REPORT_TOKENS` — the section headings, table headers, labels and footer
  phrases, keyed language → phrase. A language with **no table**, and a table
  **missing one of `REQUIRED_REPORT_TOKENS`**, are the same defect: both are
  refused with one model-readable line naming the language, and nothing is
  written.
- `REASON_TOKENS` — every `confidence_reasons` entry and every
  `seasonality.note`, keyed language → the **verbatim English constant** from
  `analyze_trends.py` → localized phrase. An unmapped reason is refused the same
  way. The keys are imported **by reference**, so a constant renamed upstream
  fails this completeness check instead of silently falling back to English.

**English is never a fallback.** A Ukrainian report with one English heading is a
bilingual artefact the reader did not ask for, and no test of the arithmetic
would catch it — the numbers would all be right. This is the same argument
`charts.v1` §7.4 carries for chart text, and the same refusal.

Mapping an English reason string to a localized phrase is **not** a number
derivation, and therefore does not breach §7.3's never-recompute rule: no value
is recomputed, only a word is chosen.

**The accepted-language intersection is named here, because it is not the union.**
A supported report language is one `REPORT_TOKENS` covers **and**
`make_charts.chart_tokens` accepts. The two sets are not equal in v1: the chart
stage accepts `ja`, and this table does not. A `ja` spec therefore produces a
valid `charts.json` — with a disclosed tofu-box font gap — and is then **refused
at the report stage with exit 1 and nothing written**. That is the correct
answer, and this paragraph is the named **Phase 7 constraint**: `run_all.py` must
**not** treat a successful chart stage as a report-stage precondition, because
the two stages do not accept the same language set in v1.

### 8.4 Numbers, null growth, and the volume base

Every displayed number is read **verbatim** from `metrics[...]` or
`charts[...]`. **Formatting is permitted; deriving is not.** A grouping
separator, a sign, a percent suffix and a fixed number of decimals are display
choices over a value already read. A second mean, a re-estimated anomaly, a
re-derived percentage and a re-ranked series are all derivations, and all are
forbidden here for the reason §7.3 gives: the value the manifest already carries
must never be derived a second time downstream.

- A `clean.pct` is displayed and the raw `pct` **never** is, for exactly the
  reason the growth chart plots only the clean variant: the raw value contains
  the spike the clean variant removed, and printing both makes the excluded
  anomaly look like part of the finding.
- A `null` window renders as the localized **not-computable** token followed by
  that window's own `reason` — **never `0`, never `0.0`, never an em-dash, never
  an empty cell**. A `0` would read as "no growth" (ANAL-06), and an em-dash
  would read as an absence the report never diagnosed. When the clean percentage
  is the one being reported, the `reason` read is the one **inside `clean`**.
- Every dynamic conclusion states **its window, its percentage, and the series'
  own `avg_daily_views` as the stated volume base**. A percentage without its
  base is not interpretable: `+5.7%` on 2 363 views/day and on 13 views/day are
  different facts, and the report must not let a reader assume otherwise.
- Percentages and counts are formatted with PEP 378's `,` and `+.1f` specs,
  which are **not locale-aware** — deliberately. The digits a reader sees are
  the digits the contract froze, in every language, and the module must
  therefore **never import `locale`**.

### 8.5 Images and the `2N+1` inventory

The image section lists exactly `2N+1` entries — the whole of `charts.json`, in
`charts.json`'s **published order**, with the overlay **last** (§7.2.0) — and
re-sorts nothing. A consumer joins each `filename` onto the directory it was
given. §7.2.1 already guarantees a bare relative name with no separator, no
`..` and no drive letter, so this stage adds **no second path-safety check**;
inventing a weaker one here would be a defect, not a defence.

At N=1 the inventory is **3**, not 2: the overlay is published unconditionally
(§7.2.0), so a one-series report that dropped it would carry a silent hole in
its own inventory. The report therefore never filters, deduplicates or
re-orders the list — it renders what the chart stage published.

**Documented v1 gap:** v1 ships no HTML renderer, so raw HTML inside a
spec-authored `label` is inert inside a plain-text Markdown file. It would be
**live** in an HTML renderer. The gap is stated here rather than closed with a
sanitizer, which would strip characters a legitimate label may legitimately
contain and would have to be maintained against a renderer this version does
not ship.

### 8.6 `metrics_shown` and the fidelity rule

`metrics_shown` is the **ordered** list of metric paths the document rendered,
in the frozen grammar `series[i].field` and
`series[i].growth.<window>.clean.pct`. It exists because the requirement that
"every number from `metrics.json` appears in the Markdown" **cannot be
satisfied literally**: `anomalies[]` can carry hundreds of per-day entries, and a
one-page report that printed them would not be one page. The report prints the
**count** and points at the chart that carries the detail.

Publishing the pointer list turns "every required number is present" from a
review habit into a **set comparison**. The reverse direction — that no number in
the Markdown lacks a source in `metrics.json` — is a separate property and is
tested by scanning the rendered text for numerals and matching each against the
documents.

### 8.7 Exit codes, atomicity, and publication

| Situation | Manifest | Exit |
|---|---|---|
| all sections rendered | `report.v1` published | 0 |
| invalid spec | nothing written | 2 |
| missing/invalid `metrics.json`, `charts.json` or `spec.json`, a required display field absent, non-finite value, unsupported language, publication failure | prior bytes preserved, no staging file | 1 |

**There is no `3` (partial) case**, for §7.5's reason: a report half-written is
the same defect as a half-populated `charts.json`. A reader who finds four
sections and a manifest naming six has been told something false about the
other two, and has no way to tell which. So a document that cannot be completed
fails the whole stage: `report.md` lands only after **every** section has been
assembled.

**Publication order:** `report.md` is written first and `report.manifest.json`
only after it is in place, so a manifest never names a report that is not there.

**Atomicity:** a same-directory staging file plus one `os.replace`, exactly as
`common.dump_json` does it. A publication failure leaves the prior bytes
untouched and no `report.md.*.tmp` behind.

**Concurrency assumption:** atomic replacement prevents partial files and
requests are serial within one process, but two independent processes targeting
the same `--out` are not serialized by the report stage. They are
**caller-serialized**: the caller must serialize them. Without that caller
serialization, the **last completed atomic replace wins**.

### 8.8 `report.v1` CLI contract

```bash
python scripts/build_report.py --spec out/spec.json --out out
```

Flags: `--spec` (required); `--out` (default `out`); and logging-only
`--verbose`, which adds no manifest field and changes no behavior. These three
flags are the **whole** surface, because every extra flag is a contract Phase 7's
`run_all.py` must learn to pass.

### 8.9 Report fixtures

- `tests/fixtures/metrics.example.json` — the golden two-series metrics mirror.
- `tests/fixtures/metrics.anomalies.example.json` — the spike-injected pair, so
  a non-empty `anomalies[]` is renderable.
- `tests/fixtures/spec.example.json` — the executable frozen spec, whose
  `language` is the document language the token tables are resolved against.
- The `charts.json` the test produces by running the **real** chart stage into
  a temporary output directory, rather than a committed hand-written manifest: a
  hand-written sibling manifest could drift from the emitter and the report
  would pass against a document the chart stage never writes.

No test may reach the network; every transport is injected or forbidden.
