# wikipedia-trend-agent

An [Agent Skill](https://agentskills.io/specification) that answers questions
about Wikipedia pageviews. The model authors one `spec.json`, runs one command,
and reads back `metrics.json` plus a one-page Markdown report with PNG charts.

**The honesty guarantee.** All arithmetic lives in the scripts, never in the
model's head: the pipeline publishes every number it computes, and every stage
downstream copies those numbers rather than recomputing them. Every
interpretation is gated by a transparent, integer confidence rubric whose
reasons are printed in the output, so a reader can see *why* a reading is
weak. And the numbers themselves are checked: `tools/validate_answer.py` grades
a model's answer against the run's own `metrics.json` (see
[Cheap-model eval](#cheap-model-eval-the-runbook)).

---

## The pipeline

| Stage | Script | Reads | Publishes |
|---|---|---|---|
| fetch | `scripts/fetch_pageviews.py` | the spec's window and article list | `series.csv` |
| analyze | `scripts/analyze_trends.py` | `series.csv` | `metrics.json` |
| charts | `scripts/make_charts.py` | `series.csv`, `metrics.json` | `charts.json` + PNGs |
| report | `scripts/build_report.py` | `metrics.json`, `charts.json` | `report.md`, `report.manifest.json` |

`scripts/resolve_articles.py` is a **separate, human-confirmed pre-stage** and
is *not* part of the command below: it discovers candidate articles for a topic
and a set of language editions, a human confirms one selection per project, and
only then does a spec get written. See `references/CONTRACTS.md` §6.

## Install

```bash
pip install -r requirements.txt
```

Python **3.11 or newer**. The one runtime dependency is `matplotlib>=3.11`
(the charts); the whole data layer — HTTP, caching, CSV, statistics — is
Python's standard library, which is what lets this run inside an arbitrary
agent runtime without a resolver step.

The skill is a **directory**: clone or copy `wikipedia-trend-agent/` next to
your agent's skills folder. The directory name and the `name:` in `SKILL.md`
must be identical — that is the agentskills.io activation rule, and
`tests/test_packaging.py` enforces it.

Before any network call, set a descriptive User-Agent (Wikimedia returns 403
without one):

```bash
export WTI_USER_AGENT="wikipedia-trend-agent/0.1.0 (you@yourdomain.tld) python-urllib"
```

## The one command

```bash
python scripts/run_all.py --spec out/spec.json --out out
```

| Exit | Meaning | What to do |
|---|---|---|
| 0 | all four stages succeeded | read `out/metrics.json`, `out/report.md`, `out/charts.json` |
| 2 | the spec is invalid | fix the spec against `references/CONTRACTS.md` §1 and re-run; stderr lists every violation at once |
| 1 | fatal — empty series, no fetchable chunks, an unreadable input, a refused report language, or a missing User-Agent | read stderr, fix the article slug, the window, the `quality` flag or `WTI_USER_AGENT` |
| 3 | partial — at least one series failed to fetch and at least one succeeded | read the per-series `series_id: message` stderr lines, fix that slug, re-run |
| anything else | a crash | report it verbatim; do not rewrite pipeline logic |

The orchestrator stops at the first failing stage and names it
(`run_all: stage <name> failed: …`), so no later stage runs and no later
artifact is written. The command takes exactly `--spec`, `--out` and
`--verbose`. Run an individual stage directly when you need its own options —
for example `make_charts.py --log-scale` for the opt-in log regime, which
`run_all.py` deliberately does not forward.

## Worked example — intermittent fasting, pl vs cs

The request, in the user's words:

> Compare the growth of interest in intermittent fasting in the Polish and Czech
> Wikipedias over the last two years.

The spec is committed: `assets/example.intermittent-fasting.json`.

The output below is **real output from the shipped report stage**, rendered
over the committed regression corpus (`tests/fixtures/series.example.csv` +
`metrics.example.json`) — a synthetic, deterministic dataset, *not* a live
capture. Reproducing it against the real API takes one command:

```bash
python scripts/run_all.py --spec assets/example.intermittent-fasting.json --out out
```

`out/report.md`:

```markdown
# Аналіз переглядів Wikipedia

## Висновок

- Польська: інтервальне голодування · 3M вікно: +3.5% · на основі 2,363.5 Переглядів/день
- Польська: інтервальне голодування · 1Y вікно: +16.7% · на основі 2,363.5 Переглядів/день
- Польська: інтервальне голодування · 2Y вікно: н/д (недостатньо спостережень в одному або в обох однакових за довжиною вікнах) · на основі 2,363.5 Переглядів/день
- Чеська: інтервальне голодування · 3M вікно: +5.7% · на основі 1,363.5 Переглядів/день
- Чеська: інтервальне голодування · 1Y вікно: +30.8% · на основі 1,363.5 Переглядів/день
- Чеська: інтервальне голодування · 2Y вікно: н/д (недостатньо спостережень в одному або в обох однакових за довжиною вікнах) · на основі 1,363.5 Переглядів/день

## Метрики

Дані станом на 2026-09-20

| Тема | Проєкт | Стаття | Мова | Період | Усього переглядів | Переглядів/день | Зростання | Напрям | Впевненість | Частка аномалій |
|---|---|---|---|---|---|---|---|---|---|---|
| Польська: інтервальне голодування | pl.wikipedia | Post_przerywany | pl | 2024-09-23..2026-09-20 (728) | 1,720,628 | 2,363.5 | 3M +3.5% / 1Y +16.7% / 2Y н/д (недостатньо спостережень в одному або в обох однакових за довжиною вікнах) | зростання | висока | 0.0 |
| Чеська: інтервальне голодування | cs.wikipedia | P%C5%AFst_p%C5%99eru%C5%A1ovan%C3%BD | cs | 2024-09-23..2026-09-20 (728) | 992,628 | 1,363.5 | 3M +5.7% / 1Y +30.8% / 2Y н/д (недостатньо спостережень в одному або в обох однакових за довжиною вікнах) | зростання | висока | 0.0 |

## Наскільки можна довіряти

- Польська: інтервальне голодування: висока — період щонайменше 91 день; переглядів за 30 днів щонайменше 10000; аномалій не виявлено
- Чеська: інтервальне голодування: висока — період щонайменше 91 день; переглядів за 30 днів щонайменше 10000; аномалій не виявлено

## Наступний крок

Це не прогноз і не рекомендація.

---

Джерело: intermittent_fasting_pl_cs · Дані станом на 2026-09-20 · Виміряно · Це не прогноз і не рекомендація
```

The block above is a verbatim excerpt of `out/report.md` with the `## Графіки`
section omitted for length — every line in it is byte-identical to the render,
which `tests/test_publish.py` proves by re-rendering the document on every
test run. One known v1 gap is visible in the omitted section: the overlay
chart's `note` is still the English constant `comparative view; per-series
scales differ` even in a Ukrainian document, because it belongs to neither
token table. It is tracked in the project's defect register and is a
`charts.v1`/`report.v1` token-table change, not a documentation fix.

**How to read it.** The document is in Ukrainian because the spec's
`language` is `uk`; the report stage writes only words its own token tables
provide, and refuses a language it has no table for rather than falling back to
English. `зростання` is the *trend direction* the analyzer computed — it means
the raw and the spike-excluded one-year readings agree in direction, not that
interest is rising. `н/д` is "not computable", never zero. For what
`confidence` and its reasons mean, and what you may say out loud, read
[`references/INTERPRETATION.md`](references/INTERPRETATION.md).

## The honesty contract

Three rules, and they are rules rather than advice:

1. **`pct: null` means *not computable*, never `0`.** A null always carries a
   reason beside it. A reader who sees `0` concludes the value was measured and
   found to be nothing; what actually happened is that it was never in a
   position to be measured.
2. **A percentage is meaningless without its volume base.** `+5.7%` on 2,363.5
   views/day and `+5.7%` on 13 views/day are different facts wearing the same
   number, and only one of them survives the noise floor.
3. **Article interest is not product demand, and neither is willingness to
   pay.** Nothing here is a forecast. Every number describes days that have
   already completed.

`references/DATA_CAVEATS.md` has the five caveats in full — the 2015-05-01
methodology break, the low-volume noise floor, spike vs growth, `null` vs
zero, and the CJK chart-font limitation.

## Environment variables

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `WTI_USER_AGENT` | yes, before any network call | a placeholder that is **refused** | descriptive identity: product, version, a real contact address, the transport; ≤256 code points |
| `WTI_CACHE` | no | `.cache` | cache directory; created on demand |
| `WTI_TTL_HOURS` | no | `24` | cache freshness in hours; `0` forces a refetch |

A placeholder User-Agent (anything containing `contact` or `example.org`) is
refused before any request is made, so a placeholder identity can never reach
the network. Use your own address, or a contact alias if you would rather not
publish a personal one.

## Cheap-model eval (the runbook)

The claim this repository makes is that a **cheap or free model can use this
skill without inventing a number**. This is the part that checks it.

**The command:**

```bash
python tools/validate_answer.py --answer out/answer.md --out out
```

**Where the answer file goes:** anywhere; `out/answer.md` is the convention.

**How to produce one.** Run the pipeline as above, then give the cheap model
`out/metrics.json`, `out/report.md`, and the instructions in `SKILL.md` plus
`references/INTERPRETATION.md`, and ask it to answer the user's question in the
user's language. Save its reply as `out/answer.md`. The model is supposed to
read, not to compute.

**Candidate models.** A cheap model is the *right* test here precisely because
it is the constraint: if the skill only works on an expensive model, it does not
work.

- **Claude Haiku 4.5** — the small tier of a frontier family, and the closest
  common stand-in for "a competent model on a budget".
- **The free OpenRouter tier** — see OpenRouter's
  [free model routing guide](https://openrouter.ai/docs/features/model-routing)
  for the current list. This is the true bottom of the market and the most
  honest test of whether the skill's instructions carry the answer on their own.

**How to read the verdict.** Exit 0 means four `PASS` lines. Exit 1 means at
least one `FAIL <property>: <detail>` line, where the detail names the offending
number with its line and offset, or the unhedged sentence. Exit 2 is the tool's
own usage problem, never the answer's.

The four properties:

| Property | What it checks |
|---|---|
| `numbers_present` | every number in the answer is present in that run's `metrics.json` |
| `low_is_hypothesis` | a `low`-confidence series is phrased as a hypothesis, never as a conclusion |
| `as_of_present` | the data-as-of date is stated, in its one canonical spelling |
| `sources_and_limitations` | both sections are present *and* have a non-empty body |

**What each failure means, and what to do:**

- `numbers_present` — the model computed something. Usually a percentage it
  derived from two metrics values, which is a new number and therefore a
  failure by design. Fix the prompt, or `references/INTERPRETATION.md`; do
  **not** loosen the check. There is no tolerance band, and adding one is
  exactly what would let a wrong number through.
- `low_is_hypothesis` — a wording failure. The BEFORE/AFTER pairs in
  `references/INTERPRETATION.md` exist to prevent exactly this; quote them at
  the model.
- `as_of_present` — a prompt omission. The model did not say how fresh the
  reading is.
- `sources_and_limitations` — a prompt omission, or an empty heading written
  for compliance.

Every failure also writes a stub under `out/eval/regression/`, carrying the
answer bytes' digest, the sha256 of the `metrics.json` it was graded against,
the offending token and the validator's version. Promote a real one to
`tests/fixtures/eval/` and commit it: that is how a failure becomes a
regression test.

**Scope, stated plainly.** This repository ships the validator and this
runbook, **not a client**. The live cheap-model run is the owner's step; no
code here performs it, and no code here holds a key or reaches the network.
That is why EVAL-01 closes `partial` by design rather than by omission.

## Sources and data

- **Wikimedia Pageviews API** — <https://wikimedia.org/api/rest_v1/metrics/pageviews/>
  and the API etiquette and rate-limit pages on
  <https://meta.wikimedia.org/wiki/API:Etiquette>. The AQS per-article endpoint
  is the only data source.
- **Agent Skills specification** — <https://agentskills.io/specification>.

What is read: public aggregate pageview counts and public search metadata.
There are **no writes**, so Wikipedia's bot policy does not apply.

Licensed under **Apache-2.0**; the full text is in [`LICENSE`](LICENSE). The
`name` == directory-name rule, the progressive-disclosure link graph and the
`references/` set are described in `SKILL.md` and enforced by
`tests/test_packaging.py`.

## Develop it further

The original ask is in `raw/task.md`. Concretely:

- **Add a language.** Four fail-closed tables own the words:
  `REPORT_TOKENS`, `REASON_TOKENS`, `CONFIDENCE_TOKEN_KEYS` and
  `TREND_TOKEN_KEYS` in `scripts/build_report.py`, plus `CHART_TOKENS` in
  `scripts/make_charts.py`. A language missing from any of them is **refused**,
  not served in English — a partial addition is a refusal, not a fallback. The
  accepted set is the *intersection* of what the chart and report stages accept,
  which is why `ja` produces a valid `charts.json` and then a refused report.
- **Add a chart kind.** Two frozen rules must both hold: the `2N+1` inventory
  in `references/CONTRACTS.md` §7.2.0, and the per-entry key partition in
  §7.2.2. `CONTRACTS.md` is a frozen contract, so a new kind is a contract
  amendment **first** and an implementation second.
- **Add a metric.** Also a contract change: `references/CONTRACTS.md` §2, then
  `tests/test_contracts.py`, then the report's `REPORT_TOKENS` and the
  `metrics_shown` grammar in §8.6.
- **Run the tests.** No network, no fixtures written, ~2.5 minutes:

  ```bash
  cd wikipedia-trend-agent && python -m pytest -q
  ```

  And the typing gate, which now covers `tools/` as well as `scripts/`:

  ```bash
  python -m mypy scripts tools --strict
  ```

- **Rehearse a clone.** The file-set half runs in the suite. The install half
  is one owner command — a real `pip install` inside a unit test is minutes of
  network and would break the suite's zero-network contract:

  ```bash
  python tools/clean_clone_check.py --full --dest /tmp/wta-clone
  ```

**Standing rules.** No value is recomputed or transformed downstream of the
stage that published it (ANAL-06). No tolerance bands on checked numbers. No
new runtime dependency without an explicit decision — one package is the whole
point of the dependency budget.

## Status

**v0.1 does:** fetch → analyze → chart → report in one command, with a
human-confirmed article-resolution pre-stage, a frozen JSON contract
(`references/CONTRACTS.md`), a four-property answer validator, and one-command
clone rehearsal.

**v0.1 deliberately does not:**

- **PDF.** The `report.manifest.json` `formats` array is the ready-made slot;
  a PDF is one appended `{format, filename}` object and one more file, not a
  rename of the Markdown slot.
- **Cross-series rollups.** The report deliberately publishes no number that
  combines series, because any such number is a second derivation.
- **Forecasting.** Nothing here projects; the field is a measured past window.
- **A `ja` report.** The chart stage accepts `ja`; the report stage does not.
  That asymmetry is documented in `references/CONTRACTS.md` §8.3 and is the
  reason `run_all.py` does not treat a successful chart stage as a report
  precondition.
