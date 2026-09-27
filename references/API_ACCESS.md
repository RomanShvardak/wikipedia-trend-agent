# API access, rate limits and the cache

Everything needed to run this skill without violating Wikimedia's rules.

This file covers **how to talk to the API and how the client behaves**: the
User-Agent policy and its fail-fast, the throttle, the retry and status-code
policy, the 404 taxonomy, and the on-disk cache. It deliberately contains
**no JSON field tables** — the request and response shapes are frozen in
`references/CONTRACTS.md` (§1 for the spec, §6 for the resolver, and the AQS
response shape the fetch stage validates), and a second copy here would be a
second thing to keep in sync.

---

## User-Agent

`WTI_USER_AGENT` must be descriptive, in one line, at most 256 code points: a
product token, a version, a **real contact address**, and the transport in use.

```bash
export WTI_USER_AGENT="wikipedia-trend-agent/0.1.0 (you@yourdomain.tld) python-urllib"
```

Wikimedia returns **HTTP 403** to clients without a descriptive identity, and
`common.user_agent()` refuses to issue a request at all unless the value is

- non-empty and not whitespace-only,
- free of control characters,
- at most 256 code points, and
- **not a placeholder** — anything containing `contact` or `example.org`.

The refusal is a `SystemExit` carrying an actionable message, and it happens
*before* any network call, so a placeholder identity can never leak onto the
wire. `run_all.py` surfaces both the message and a non-zero exit, naming the
stage that failed.

Put **your own** address in it. If you would rather not publish yours, use a
contact alias, a form address, or a mailing list you control — but put a real
one there. Never commit a personal address you did not intend to publish.

## Throttle

~1 request per second, serial within a process. Every request passes the
throttle before it is issued, so a five-series two-year run is paced rather
than bursted.

The ~1-year AQS chunk limit is **politeness, not a server cap**: the API accepts
longer windows, and the client chunks at ≤365 days per request anyway. That
chunking is a correctness decision (a long window that fails halfway is a long
retry) as much as a politeness one.

## 429, `Retry-After` and 5xx

- At most **3 attempts** per request.
- `Retry-After` is honoured as **integer seconds** or as an **HTTP-date**.
- Anything unparseable, non-positive, or absent falls back to **5.0 seconds**.
- **5xx** is retried on the same policy.
- Any other non-200 is a failure for that request, not a retry.

## 403

**Immediately fatal, never retried.** The cause is a missing or
non-descriptive `WTI_USER_AGENT`, or a Wikimedia-side block. The action is to set
`WTI_USER_AGENT` to a descriptive value and re-run.

The distinction from 429 matters and is not cosmetic: **429 means slow down,
403 means fix your identity.** A retry loop can fix the first and can never fix
the second.

## The 404 taxonomy

A 404 is classified by **how far the requested window's end is from the last
complete UTC day**:

| Distance | Classification | What is written |
|---|---|---|
| ≤ 2 days | `not_loaded` | nothing — the data is not published yet; retry later |
| > 2 days | `no_views` | one truthful `0` row for every day of the chunk |

`not_loaded` is never a zero and never an error in the data. Treating "not
published yet" as "no interest" is the single most damaging misreading the
fetch stage is built to prevent, which is why the classification exists at all
and why the two states are not merged.

Under `quality.fail_on_empty_series` (the default in the shipped example
spec), a series whose chunks all come back `not_loaded` is fatal, after the
full scan, rather than silently producing an empty series.

## The cache

- Location: `.cache/` by default; `WTI_CACHE` moves it.
- Key: **SHA-256 of the full request URL**; the filename is `<key>.json`.
- Envelope: exactly `{fetched_at, status: 200, response}`.
- **Only a validated HTTP 200 is ever written.** An envelope that fails
  validation — wrong status, missing `fetched_at`, a non-list `items` — is
  *deleted*, not trusted, and the request is re-issued.
- TTL: `WTI_TTL_HOURS`, or 24 hours. `--ttl-hours 0` (and `WTI_TTL_HOURS=0`)
  bypasses reads and forces a refetch, while keeping the same key and the same
  write envelope.
- Reset: `rm -rf .cache`. There is no other invalidation mechanism, and there
  does not need to be one at this scale.

The cache is the primary defence against re-fetching a window you already have,
and it is why a repeated or refining question costs no additional requests.

## Bounded display strings: 256 code points

`resolved.json` keeps every externally-supplied or human-readable string
**bounded at 256 code points and free of C0/C1 control characters** (D-04).
The bound is a family rule, not one flag's quirk, and it applies to:

| Input | Rule |
|---|---|
| `--reason` | at most 256 code points, as one string |
| `--topic-for PROJECT=QUERY` | at most 256 code points for `PROJECT` + `QUERY` together |
| any `article` / `title` / `message` in the manifest | the same bound |

Three things worth knowing before you hit the limit:

1. **Code points, not bytes.** `len()` on a Python `str` counts code points. A
   Ukrainian or Czech justification of 200 characters is ~400 UTF-8 bytes but
   still 200 code points. A reader who assumes bytes will misjudge a Cyrillic
   `--reason` by exactly 2×.
2. **Over-long input is refused, never truncated.** A truncated justification
   is a false record of why an article was chosen, so the input must be
   rejected. The error message names the bound and the remedy, so a refusal is
   self-explanatory:
   `must be bounded and control-free (at most 256 code points, no C0/C1
   controls; how to fix: keep --reason under 256 code points)`.
3. **One sentence per project is the practical limit.** A `--reason` is a record
   of a human decision, not an essay; the worked example in `manual.md` §11 is
   183 code points for two projects.

The number is interpolated from `MAX_DISPLAY_CODE_POINTS` into every message,
so the text can never claim a bound the code does not enforce.

## The 2026 REST-Gateway limits

The Wikimedia REST Gateway began deploying rate limiting in early 2026. For
callers with a compliant User-Agent the published budget is **≤200 requests per
minute** and **≤3 concurrent connections**.

This client is serial and throttled to ~1 request/second, so it operates well
under both numbers. **429 should not appear in normal use** — the retry policy
in this file is written defensively, not because 429 is routine. If you do see
429s, something else is generating traffic under your identity.

## Environment variables

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `WTI_USER_AGENT` | yes, before any network call | a placeholder that is **refused** | descriptive identity sent as `User-Agent`; ≤256 code points, no placeholder |
| `WTI_CACHE` | no | `.cache` | cache directory; created on demand |
| `WTI_TTL_HOURS` | no | `24` | cache freshness in hours; `0` forces a refetch |

No API key, no token and no account is involved. The only credential this skill
has is a way for a human to be contacted about the traffic.

## Sources

Wikimedia API etiquette and rate-limit policy: *MediaWiki API: Etiquette* and
*API:Etiquette/Rate limits* on meta.wikimedia.org, and the Wikimedia REST
Gateway documentation. The resolver's own contract for these behaviours —
including its exit codes — is `references/CONTRACTS.md` §6.4.
