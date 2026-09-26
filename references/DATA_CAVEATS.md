# Data caveats

What makes these numbers easy to misread.

Five caveats, each stated as: what it is, how it shows up in the data, and what
a reader must therefore not conclude. This file deliberately contains **no
field tables** — `references/CONTRACTS.md` owns the frozen shapes, and
`references/INTERPRETATION.md` owns the vocabulary of `confidence`,
`trend_direction` and `pct`. What follows is only the judgement those two
cannot make for you.

---

## 1. The 2015-05-01 methodology break

Before **2015-05-01** Wikimedia filtered spiders and bots differently, so
counts either side of that date are not produced by the same instrument. A
window whose **comparison** crosses the break is not a like-for-like
comparison, no matter how clean the raw series looks.

How it shows up: the analyzer detects the crossing, subtracts **2** from the
confidence score and adds the reason `comparison crosses 2015-05-01 methodology
break`. It is the only term that subtracts points.

What not to conclude: a pre-2015 series is not a trend. A 2024–2026 window is
entirely unaffected; if your reasons carry the crossing, say so in the same
sentence as the percentage rather than in a footnote.

## 2. The low-volume noise floor

Below **1000 views per 30 days** the reading is noise, not signal.

How it shows up: `trend_direction` is forced to `noise` regardless of what the
percentages say, and the confidence rubric awards no volume points for it (the
reason `monthly 30-day views below 1000` appears).

What not to conclude: a dramatic percentage on 9 views/day is an arithmetic
artefact of a small denominator. `+400%` on 9 views/day is 45 views/day, and it
means nothing about a topic.

## 3. Spike vs growth

One viral day is an entry in `anomalies[]`, not a trend.

How it shows up: anomalies are detected with a **rolling median + scaled MAD**
(radius 3 days, scale 1.4826, k = 3.5) *before* the growth windows are
computed. Every window therefore carries a `clean` variant with the anomalous
days removed.

What not to conclude: that a spike is part of the trend. **Report the clean
percentage**; the raw one contains the spike you are supposed to be
discounting, and printing both makes the excluded anomaly look like part of the
finding. `anomaly_share` above 5% earns no confidence bonus and always appears
in the reasons.

## 4. `unavailable` and `null` vs numeric zero

In the resolver, a 404 gives `status: "unavailable"` with `total_views: null`
and a null `low_volume` — **never `0`**. In the metrics, a non-computable
window gives `pct: null` with a `reason` — **never `0`**.

These are the same rule seen from two directions, and both exist for one reason:
**a zero reads as a measurement.** A reader who sees `0` concludes that the
value was measured and found to be nothing. What actually happened is that it
could not be measured — the data is not published yet, the window is too short,
or the comparison window's mean is exactly zero.

What not to conclude: never that "0" and "not computable" are close. They are
different claims, and only one of them is a fact about the world.

## 5. The CJK-font limitation

The bundled DejaVu font covers Latin and Cyrillic. A non-Latin script such as
Chinese or Japanese renders as **tofu boxes**, with a single stderr warning
naming the language, and the chart discloses a masked-point count. This is
recorded as a documented v1 gap in `references/CONTRACTS.md` §7.4; there is **no
font fallback in v1**.

The related fact is not a font problem at all: the **chart stage accepts more
languages than the report stage does**. A `ja` spec produces a valid
`charts.json` and is then refused at the report stage with exit 1 and nothing
written. Do not read the chart success as a run success — read the stderr line
naming the language.

---

## What this is not

Two standing boundaries, both of which belong in the conclusion of any answer
this skill helps you write:

- **Article interest is not product demand, and neither is willingness to
  pay.** Pageviews measure attention to a Wikipedia article. They do not
  measure customers, revenue, or intent to buy.
- **This is a description of a measured past window, not a forecast of a future
  one.** Every number here describes days that have already completed. A
  `trend_direction` of `up` is a statement about a comparison between two
  windows, not a projection.
