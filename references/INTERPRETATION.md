# Interpreting `metrics.json`

What a reading means, what it does not mean, and what you may say out loud.

This file answers the four questions a model has after the pipeline has
produced `metrics.json`: what `confidence` is, what `trend_direction` is and
is not, why `pct: null` is never `0`, and how to phrase a `low` reading. It
deliberately contains **no field tables** — those live in
`references/CONTRACTS.md` (§2 is `metrics.json`) and are not restated here,
because a second copy of a frozen contract is a second thing that can drift.
It also does not describe the JSON shapes of the chart or report manifests;
§7 and §8 own those.

Every reason string quoted below is a **verbatim English constant** from
`scripts/analyze_trends.py`, and `tests/test_packaging.py` greps this file for
each one by importing the constant. If a constant is renamed upstream, that
test fails here rather than letting this file go quietly stale.

---

## 1. What `confidence` is

`confidence` is **not** a vibe and **not** a model judgement. It is one of
`high`, `medium`, `low`, produced by an integer rubric computed in
`analyze_trends.score_confidence` over four terms. You can reconstruct the
score from `confidence_reasons`, and the reasons are the audit trail.

The arithmetic, exactly as the module computes it:

| Term | Condition | Points | Reason emitted |
|---|---|---|---|
| Period | `period.days >= 730` | `+2` | `period at least 730 days` |
| Period | `91 <= period.days < 730` | `+1` | `period at least 91 days` |
| Period | `period.days < 91` | `+0` | `period below 91 days` |
| Volume | 30-day mean views `>= 10000` | `+2` | `monthly 30-day views at least 10000` |
| Volume | `1000 <= 30-day mean < 10000` | `+1` | `monthly 30-day views at least 1000` |
| Volume | 30-day mean `< 1000` | `+0` | `monthly 30-day views below 1000` |
| Anomalies | `anomaly_share == 0` | `+1` | `no anomalies detected` |
| Anomalies | `0 < anomaly_share <= 0.05` | `+0` | `anomaly share within 5 percent` |
| Anomalies | `anomaly_share > 0.05` | `+0` | `anomaly share above 5 percent` |
| Methodology | comparison window crosses 2015-05-01 | `−2` | `comparison crosses 2015-05-01 methodology break` |
| Disclosure | the clean 1Y variant is unavailable | `+0` | `clean 1-year growth unavailable` |

The bands:

- `score >= 4` → **`high`**
- `2 <= score < 4` → **`medium`**
- `score < 2` → **`low`**

A `low` series additionally carries the reason
`low confidence: treat the reading as a hypothesis`.

Three things worth knowing before you quote it:

1. **`clean 1-year growth unavailable` costs no points.** It is a
   *disclosure*, not a penalty: the analyzer is telling you the spike-excluded
   1Y comparison could not be computed, and you must say so rather than
   silently falling back to the raw percentage.
2. **The reason list can be longer than the score has terms.** The disclosure
   above adds a reason without adding a term, and a `low` verdict adds one
   more. So **never reconstruct a level from the number of reasons** — count
   the points using the table, or quote the level the module already emitted.
3. **The crossing penalty is the only term that subtracts.** A 2015-crossing
   window can be `low` on a two-year, high-volume, anomaly-free series purely
   because of it. Say so explicitly rather than presenting it as weak data.

### The `seasonality.note` vocabulary

`seasonality.note` is one of three constants, and it means something different
from a confidence reason — it describes whether recurring peak months could be
identified at all, and it is never a score term:

- `fewer than two aligned 365-day halves are available` — the window is too
  short to compare two aligned halves, so no seasonality claim is made at all.
- `no recurring peak months identified` — two aligned halves exist and no
  month effect survived the test.
- `recurring peaks have positive shared month effects whose post-subtraction residual is non-negative in both aligned halves, so a month effect measured in only one of the two observed years is not reported as recurring; month-scale monotonic curvature outside the fitted linear/quadratic daily trend can still be confounded with calendar effects across only two observed cycles` — peaks *were* found, and this states exactly how much weight they carry: two observed cycles cannot separate a seasonal effect from calendar curvature.

Report a seasonality note by quoting it, not by summarising it into "seasonal"
or "not seasonal".

---

## 2. What `trend_direction` is, and is not

Five values, and only five: `up`, `down`, `flat`, `noise`, `inconclusive`.

The gates, in the order `analyze_trends.safe_direction` applies them:

1. `period.days < 91`, **or** the clean 1Y percentage is `null` →
   **`inconclusive`**. There is not enough comparable data.
2. `confidence == "low"`, **or** the 30-day mean is under 1000 views →
   **`noise`**. The numbers are too small or too weak to mean anything.
3. The raw 1Y and the clean 1Y percentages land in **different** ±10% bands →
   **`noise`**. A single spike is enough to flip the sign, and that is exactly
   what the clean variant exists to expose.
4. Otherwise the clean band is returned: `up` above +10%, `down` below −10%,
   `flat` in between.

So `up` means: *the raw and the spike-excluded readings agree in direction, and
the period, confidence and volume gates all passed.* It never means "interest is
rising", and it never means "this topic is worth building". Those are different
claims, and the second one is not in the data at all.

`inconclusive` and `noise` are **answers, not failures**. Report them as
findings. "The direction is inconclusive over this window" is a true sentence;
silently omitting the series, or reporting its raw percentage as a direction,
is not.

---

## 3. Why `pct: null` is not `0`

`references/CONTRACTS.md` §2.1 freezes this: a null `pct` means **not
computable**, never zero, and it always carries a `reason` beside it — inside
the `clean` object when the clean variant is the one being reported.

The analyzer emits exactly two reasons:

| Reason | What it means |
|---|---|
| `insufficient observations in one or both equal-length windows` | one or both of the two equal-length windows does not have enough present days to be compared. A 2Y window needs 730 days **plus** its own 730-day comparison, so a 2-year spec cannot compute `growth.y2`. |
| `previous equal-length window has zero mean` | the comparison window's mean is exactly zero, where a percentage is mathematically undefined rather than small. |

**The wording rule.** Quote the `reason` verbatim, or say the window is not
computable. Never write `0`, `0%`, `0.0`, an em-dash, or leave the cell empty.
Each of those is a *measurement* a reader will believe: `0%` says "the value did
not change", and a not-computable value did not fail to change — it was never
in a position to be measured. The same rule governs the report, which prints
the localized `not_computable` token (`н/д` in Ukrainian) rather than a number.

---

## 4. The wording rules for a `low` reading

These are binding on the answer, not stylistic advice. A `low` series is a
**hypothesis**: it is phrased with a hedge, it carries its confidence level and
its reasons, and it is never a conclusion, never a recommendation, and never
the lead sentence of an answer.

Ukrainian (the committed spec's language):

| Do not write | Write instead |
|---|---|
| Інтерес до інтервального голодування зростає на 5,7%. | За чистим 1R інтерес у польській Вікіпедії +5,7% на базі 2363,5 переглядів/день; впевненість низька, тож це гіпотеза, а не висновок. |
| Тема втрачає популярність. | За чистим 1R інтерес у чеській Вікіпедії −3,1% на базі 412,0 переглядів/день; впевненість низька через 8% аномалій — напрям не стійкий. |
| Дані ненадійні, краще не робити висновків. | Впевненість низька: період 60 днів коротший за 91 день, перегляди за 30 днів 210, нижче порогу 1000. Це твердження про коротке вікно, а не тренд. |

English (the same two rules, for an English-language answer):

| Do not write | Write instead |
|---|---|
| Interest in intermittent fasting is rising by 5.7%. | Clean 1Y interest in the Polish edition is +5.7% on a base of 2363.5 views/day; confidence is low, so this is a hypothesis rather than a conclusion. |
| The data is unreliable, no conclusions possible. | Confidence is low: the window is 60 days (below the 91-day minimum) and 30-day views are 210, under the 1000 floor. That is a statement about a short window, not a trend. |

Two hard prohibitions that apply to every reading, not only a `low` one:

1. **Quote the `clean` percentage, never the raw one.** The raw value contains
   the spike the clean variant removed; printing both makes the excluded
   anomaly look like part of the finding. `references/CONTRACTS.md` §8.4 froze
   exactly this: the clean value is displayed, the raw one never is.
2. **Always state the volume base beside the percentage.** `+5.7%` on 2363.5
   views/day and `+5.7%` on 13 views/day are different facts wearing the same
   number, and only one of them survives contact with the noise floor.

And the boundary that no reading may cross: **article interest is not product
demand, and neither is willingness to pay.** Say so in the conclusion. This
skill measures attention to a Wikipedia article over a window that has already
happened; it does not forecast, price, or recommend.
