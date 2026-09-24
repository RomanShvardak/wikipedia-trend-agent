# Wikipedia Trend Agent

Agent Skill, що аналізує тренди переглядів Wikipedia (Wikimedia Pageviews API):
порівнює інтерес до тем і мовних розділів, виявляє сезонність та аномалії,
обчислює зростання 3M/1Y/2Y (із варіантами без викидів) і будує один
Markdown-звіт з PNG-графіками — на основі frozen-контрактів.

**Status: Phase 1 skeleton — контракти заморожені (v0.1), етапи пайплайну
надходять у Phases 2–7.**

## Install

Python ≥ 3.11 (код використовує `list[...]`-типи). Одна команда:

    pip install -r requirements.txt

## Run tests

    cd wikipedia-trend-agent && python -m pytest -q

## Environment overrides

| Variable | Default | Meaning |
|---|---|---|
| `WTI_USER_AGENT` | — | Описовий User-Agent, обов'язковий перед будь-яким мережевим викликом (fail-fast на джерельно-невизначеному значенні) |
| `WTI_CACHE` | `.cache/` | Директорія кешу (sha256-keyed JSON-файли) |
| `WTI_TTL_HOURS` | 24 | TTL кешу в годинах |

## Contracts

- `references/CONTRACTS.md` — заморожені контракти `spec.json` та
  `metrics.json` (v0.1): всі поля, семантика `pct: null` (never 0), вихідні
  коди, правило «без міжсерійних агрегатів».

Назва директорії точно дорівнює полю `name` у фронтматтері `SKILL.md`
(`wikipedia-trend-agent`) — вимога agentskills.io, яку перевіряє
`skills-ref` (Phase 7).