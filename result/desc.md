# wikipedia-trend-agent — детальний опис

**Тип:** Agent Skill ([agentskills.io/specification](https://agentskills.io/specification))
**Версія:** 0.1.0 · **Ліцензія:** Apache-2.0 · **Python:** ≥ 3.11
**Рантайм-залежність:** одна — `matplotlib>=3.11`
**Аудиторія цього документа:** змішана. Розділи 1–3, 10, 12 — для тих, хто приймає рішення (не потрібно знати Python). Розділи 4–9, 11 — повний технічний розбір для розробника.

> **Термінологія.** Терміни, імена полів, команди, коди помилок і файли залишено англійською — вони мають точні значення в коді й контракті. Пояснювальний текст — українською.

---

## Зміст

1. [Що це таке](#1-що-це-таке)
2. [Кому це потрібно і які задачі розв'язує](#2-кому-це-потрібно)
3. [Сценарії використання](#3-сценарії-використання)
4. [Архітектура](#4-архітектура)
5. [Вхідний контракт `spec.json`](#5-вхідний-контракт-specjson)
6. [Резолвер статей: `resolve_articles.py`](#6-резолвер-статей-resolve_articlespy)
7. [Стадія fetch: `fetch_pageviews.py`](#7-стадія-fetch-fetch_pageviewspy)
8. [Стадія analyze: `analyze_trends.py`](#8-стадія-analyze-analyze_trendspy)
9. [Стадії charts і report](#9-стадії-charts-і-report)
10. [Чому цим можна довіряти](#10-чому-цим-можна-довіряти)
11. [Якість, тести й оцінка дешевої моделі](#11-якість-тести-й-оцінка-дешевої-моделі)
12. [Експлуатація та розвиток](#12-експлуатація-та-розвиток)
13. [Додатки](#13-додатки)

---

## 1. Що це таке

`wikipedia-trend-agent` — це навичка (Agent Skill) для AI-агента, яка відповідає на запити про **зростання інтересу до тем і мовних розділів Wikipedia** за даними Wikimedia Pageviews API.

Механіка в одному реченні: **модель пише один файл `spec.json`, запускає одну команду, читає один файл `metrics.json` і формує відповідь, не виконуючи жодних обчислень.**

```
запит користувача
      ↓
resolve_articles.py   (2 виклики: discovery → confirm)   → out/resolved.json
      ↓  модель пише spec.json на основі підтверджень
run_all.py --spec out/spec.json --out out
      ↓
 fetch → analyze → charts → report
      ↓
out/metrics.json  +  out/report.md  +  out/charts.json  +  PNG-графіки
      ↓
відповідь агента (усі числа цитаються, не вираховуються)
```

**Ключова ідея — розділення праці між моделлю і кодом.** Модель добре формулює запит і погано рахує. Тому:

| Задача | Хто виконує |
|---|---|
| Зрозуміти запит, знайти статтю, поставити уточнювальне питання | модель |
| Завантажити, порахувати зростання, знайти аномалії, побудувати графіки, сформувати звіт | код |
| Скласти відповідь, додавши контекст і чесні застереження | модель |

Арифметика повністю в коді. Модель **цитує** числа, а не перераховує їх — це заборонено правилом ANAL-06 і перевіряється автоматично (§11).

**Чого агент принципово не робить:** не прогнозує попит, не оцінює здатність платити, не пише у Вікіпедію, не використовує API-ключів. Усі числа описують **вже завершені дні** минулого вікна.

---

## 2. Кому це потрібно

**Основний аудиторія — B2C-засновник**, який вирішує, які теми або мовні розділи розвивати: чи є сенс вести блог про інтервальне голодування польською, чи варто тримати чеський контент, де конкуренція нижча.

**Другий аудиторія — аналітик/дослідник**, якому потрібно швидко зрозуміти, чи зростає інтерес до теми, і наскільки можна довіряти цьому зростанню.

**Третій — агент-інженер**, якому потрібна дешева навичка, яка працює без pandas/numpy/requests, без ключів і без додаткової інфраструктури.

### Чому саме Wikipedia

Pageviews — це **безкоштовний, публічний, історично глибокий** (з 2007 р.) показник уваги до теми. Він не вимірює продажі, але дає первинний сигнал: чи взагалі існує аудиторія, чи вона зростає, чи є сезонність, чи це разовий спайк. Для B2C-засновника, який ще не має власних даних про попит, це найдешевший доступний орієнтир — за умови, що його правильно прочитати. Якраз із «правильним прочитанням» і допомагає агент.

---

## 3. Сценарії використання

### Коли вмикати

| Запит користувача | Що робить агент |
|---|---|
| «Чи зростає інтерес до теми X?» | 1 серія, вікно 3M/1Y/2Y, напрям + впевненість |
| «Порівняй інтерес у польській і чеській Wikipedia до теми X» | 2+ серії, `chart_overlay.png` зі співставленням |
| «Яку мову розвивати наступною?» | кілька мовних розділів однієї теми, порівняння `clean` зростання |
| «Наскільки можна довіряти цьому тренду?» | розділ `confidence` + `confidence_reasons` + `anomalies[]` |
| «Чи є сезонність у цій темі?» | `seasonality.months` (лише за двохрічного вікна) |
| «Порівняй два періоди: 2023 і 2025» | два запуск або ширше вікно + чесна примітка про сезонність |

### Коли не вмикати

- Прогноз продажів, виручки, оцінка TAM → pageviews ≠ попит.
- ПорівнянняRaw-даних замість `clean` — агент свідомо приховує спайки.
- Точне відтворення застарілих даних (pre-2015) — методологія змінювалася, агент це позначає штрафом.
- Запити, що стосуються реального стану Вікіпедії, а не переглядів.

### Межа довіри, яку агент озвучує в кожному звіті

```
Це не прогноз і не рекомендація.
```

Цей рядок — частина frozen-контракту звіту (`## Наступний крок`), а не стиль редакції. Він присутній у кожному згенерованому `report.md`.

---

## 4. Архітектура

### 4.1 Склад репозиторію

```
wikipedia-trend-agent/
├── SKILL.md                          232 рядки — інструкція для агента
├── README.md                         398 рядків — для людини
├── LICENSE                           Apache-2.0
├── pyproject.toml                    ruff / mypy / pytest
├── requirements.txt                  matplotlib>=3.11
├── scripts/
│   ├── common.py                     спільні утиліти (HTTP, кеш, UA, throttle, валідація spec)
│   ├── resolve_articles.py           1836 рядків — pre-stage: пошук і підтвердження статей
│   ├── fetch_pageviews.py            464  рядки  — AQS → series.csv
│   ├── analyze_trends.py             922  рядки  — series.csv → metrics.json
│   ├── make_charts.py                1865 рядків — PNG + charts.json
│   ├── build_report.py               1093 рядки  — report.md + report.manifest.json
│   └── run_all.py                    135  рядків — оркестратор чотирьох стадій
├── references/
│   ├── CONTRACTS.md                  50 KB — заморожений контракт усіх JSON
│   ├── INTERPRETATION.md             як читати результат і що можна казати
│   ├── API_ACCESS.md                 UA, throttle, retry, 404, кеш
│   └── DATA_CAVEATS.md               5 підводних каменів даних
├── assets/
│   ├── example.intermittent-fasting.json  приклад spec
│   └── wikipedia-projects.json            allowlist проєктів (wikipedia-projects.v1)
├── tests/                            14 файлів (13 test_* + conftest), 25 fixtures
└── tools/
    ├── validate_answer.py            грейдінг відповіді моделі
    └── clean_clone_check.py          репетиція чистого клону
```

### 4.2 Таблиця стадій

| Стадія | Скрипт | Читає | Публікує | Код виходу |
|---|---|---|---|---|
| fetch | `fetch_pageviews.py` | `spec.window`, `spec.series` | `series.csv` | 0 / 1 / 2 / 3 |
| analyze | `analyze_trends.py` | `series.csv` | `metrics.json` | 0 / 1 / 2 |
| charts | `make_charts.py` | `series.csv`, `metrics.json` | `charts.json` + 2N+1 PNG | 0 / 1 / 2 |
| report | `build_report.py` | `metrics.json`, `charts.json` | `report.md`, `report.manifest.json` | 0 / 1 / 2 |
| **pre-stage** | `resolve_articles.py` | Action API + AQS | `resolved.json` | 0 / 1 / 2 / 3 |

`resolve_articles.py` **не входить** у `run_all`: це підтверджуваний людиною крок, який виконується до написання `spec.json`.

### 4.3 Потік даних

```
spec.json ──fetch──> series.csv ──analyze──> metrics.json
                        │                        │
                        │                        └──> charts.json + PNG
                        └──────────────────────────────┘
                                    (charts читає обидва)
```

Ключова властивість: кожна стадія **читає артефакт, а не стан попередника в пам'яті**. Тому стадію можна перезапустити окремо з іншими опціями, а контракт між стадіями — це зміст файлів, а не домовленість у коді.

### 4.4 Як влаштований `run_all.py` (важлива деталь)

Стадії викликаються **імпортом і викликом `main(argv)`, а не spawn підпроцесу**:

```python
for stage_name, module_name in STAGES:
    code = stage_main(["--spec", args.spec, "--out", args.out])
    if code != 0:
        return code
```

Причини (зафіксовані в docstring модуля):

- один інтерпретатор → один `import matplotlib` замість чотирьох;
- немає залежності від шляху до інтерпретатора (Windows-safe);
- `--out` гарантовано один і той самий для всіх стадій;
- `transport` залишається ін'єктованим, тому весь pipeline тестується **без мережі**.

Оркестратор зупиняється на **першій** ненульовій стадії, друкує `run_all: stage <name> failed: exit <code>` у stderr і повертає код без змін. Жоден наступний артефакт не пишеться.

### 4.5 Семантика кодів виходу

| Код | Значення | Що робити |
|---|---|---|
| `0` | усі чотири стадії успішні | читати `metrics.json`, `report.md`, `charts.json` |
| `2` | **некоректний spec** | виправити spec за `CONTRACTS.md` §1 і перезапустити; stderr перелічує **всі** порушення одразу |
| `1` | fatal: порожня серія, немає chunk'ів для завантаження, нечитаний вхід, відмовлена мова звіту, відсутній `WTI_USER_AGENT` | прочитати stderr, виправити slug / вікно / `quality` / UA |
| `3` | partial: ≥1 серія не завантажилась, ≥1 успішна | прочитати рядки `series_id: message`, виправити slug, перезапустити |
| інше | crash | повідомити дослівно; **не переписувати логіку pipeline** |

Код `2` може прийти **тільки** з `SystemExit(2)` у `common.load_and_validate_spec` — жодна стадія не повертає `2` своїм результатом. Тому «spec некоректний» і «щось зламалося» не можна сплутати.

### 4.6 Чому `2` означає «виправ spec», а не «полагай код»

Це найважливіше правило експлуатації. Валідація в `common.validate_spec` **агрегована**: вона збирає всі порушення і повертає список, а не зупиняється на першому:

```
spec.json validation failed (3 errors):
- root.quality: expected an object (how to fix: use `{"fail_on_empty_series": true}`)
- series[0].label: missing required field (how to fix: add "label" like "Польська: інтервальне голодування")
- window: start (20260920) must be before end (20240923) (how to fix: swap so start < end)
```

Формат рядка: `{path}: {problem} (how to fix: {fix})`. Модель має достатньо інформації, щоб виправити spec без читання коду.

### 4.7 Технічний стек і чому він такий

| Шар | Рішення | Обґрунтування |
|---|---|---|
| HTTP | `urllib.request` (stdlib) | один синхронний GET; `requests` у maintenance mode; `httpx` важчий |
| Дані | `csv` + `statistics` + `fractions` (stdlib) | обсяг ≤ 50 000 рядків; pandas = 60 MB і 300–700 ms на import |
| Графіки | `matplotlib>=3.11`, backend `Agg`, PNG | єдина обов'язкова залежність; продукт вимагає графіки |
| Тести | `pytest>=9`, `mypy --strict`, `ruff` | dev-only; фікстури — реальні знімки API, без мережі |
| Точність | `fractions.Fraction` | середні та порівняння — **точні раціональні**, без float-помилки |

Останній рядок — не архаїзм. `Fraction` проникає всюди: середні у вікнах, `monthly_30d`, фіт сезонності, порівняння з порогами 1000/10000. `float` з'являється **лише на межі серіалізації** (`float(round(pct, 1))`). Наслідок: `+16.7%` у звіті — це не «approximately», це число, обчислене без втрати точності й округлене один раз.

---

## 5. Вхідний контракт `spec.json`

### 5.1 Дозволені поля

| Рівень | Дозволені ключі | Обов'язкові |
|---|---|---|
| root | `name`, `request`, `language`, `window`, `series`, `assumptions`, `quality` | `name`, `request`, `language`, `window`, `series` |
| `window` | `start`, `end`, `granularity` | `start`, `end` (`granularity` = `"daily"`) |
| `series[]` | `id`, `project`, `article`, `label`, `language` | усі п'ять |
| `quality` | `fail_on_empty_series` | — |

Будь-який невідомий ключ — порушення. Контракт **заморожений**: поля не можна додавати «на льоту».

### 5.2 Повний приклад

Файл `assets/example.intermittent-fasting.json` — це не декоративний шаблон, а зафіксований кейс із зафіксованими припущеннями:

```json
{
  "name": "intermittent_fasting_pl_cs",
  "request": "Порівняй зростання інтересу до інтервального голодування в польськомовній і чеськомовній Wikipedia за останні два роки.",
  "language": "uk",
  "window": { "start": "20240923", "end": "20260920", "granularity": "daily" },
  "assumptions": [
    "тема представлена однією статтею в кожному розділі — для cs.wikipedia це так, для pl.wikipedia ні (див. наступний пункт)",
    "слаги статей перевірені через resolve_articles.py: обидва існують (action=query&titles= повертає pageid, AQS відповідає 200)",
    "у pl.wikipedia немає окремої статті про інтервальне голодування. Пошук за post przerywany / głodzenie przerywane / głодówka przerywana / przerywany / 16:8 не повертає відповідної статті, а action=query&titles= повідомляє missing для Post_przerywany, Głodzenie_przerywane, Głodówka_przerywana, Post_przerywany_(dieta). Найближча наявна стаття — Głodówka_lecznicza, і вона описує голодування загалом (у т.ч. лікувальне), а не інтервальний розклад",
    "обидва слаги відтворюють те, що видає resolve_articles.py (percent-encoded, safe=\"\"), і передаються у AQS URL без жодного перекодування",
    "дві серії вимірюють НЕ строго однакову тему: cs.wikipedia/Přerušovaný půст — окрема стаття саме про інтервальне голодування, тоді як pl.wikipedia/Głodówka lecznicza — про голодування загалом. Порівняння вимірює інтерес до суміжних тем, а не до одного й того самого розкладу",
    "обидві серії нижчі за поріг шуму 1000 переглядів на місяць, тому результат низької впевненості є очікуваним і не є виправом специфікації"
  ],
  "series": [
    {
      "id": "pl-post-przerywany",
      "project": "pl.wikipedia",
      "article": "G%C5%82od%C3%B3wka_lecznicza",
      "label": "Польська: голодування загалом (найближча наявна стаття, не інтервальне)",
      "language": "pl"
    },
    {
      "id": "cs-pust-prerusovany",
      "project": "cs.wikipedia",
      "article": "P%C5%99eru%C5%A1ovan%C3%BD_p%C5%AFst",
      "label": "Чеська: інтервальне голодування",
      "language": "cs"
    }
  ],
  "quality": { "fail_on_empty_series": true }
}
```

### 5.3 Поля, які найчастіше ставлять на місце

| Поле | Примітка |
|---|---|
| `request` | Дослівний запит користувача. **Не** переписується під можливості даних. Обмеження йдуть в `assumptions`, а не в рот користувача. |
| `language` | Мова **документа звіту**, не мова даних. Визначає словники токенів. |
| `window.start/end` | Формат `YYYYMMDD` (без дефісів). `start < end`. |
| `article` | **Percent-encoded** slug, дослівно з `resolved.json`. Ніколи не кодується повторно. |
| `id` | Має бути filename-safe: `[A-Za-z0-9._-]+`. Інакше `make_charts.py` відмовляє. |
| `label` | Людське ім'я для звіту й осі графіка. Не впливає на дані. |
| `assumptions` | Потрапляє у звіт дослівно. Саме тут живе чесність про те, що дві серії — не одна тема. |
| `quality.fail_on_empty_series` | `true` (дефолт у прикладі): серія, у якої всі chunk'и повернули `not_loaded`, робить запуск fatal. |

### 5.4 Принцип «articles-first»

Тема живе **тільки** в `request` та `assumptions`. В `series[]` — лише конкретні статті. Причина практична: `article` — це те, що піде в URL AQS. Якщо дозволити моделі писати тему в `series[].article`, вона почне вигадувати слаги, які дадуть 404.

---

## 6. Резолвер статей: `resolve_articles.py`

Це найцікавіша архітектурна частина. Навіщо він: запит «інтерес до інтервального голодування в польській Wikipedia» **не має однозначної відповіді** — у pl.wikipedia такої статті немає взагалі, а найближча (`Głodówka lecznicza`) описує голодування загалом. Модель, яка просто підставить «звичайний» slug, отримає або 404, або мовчки виміряє не ту тему.

Резолвер вирішує це **двома обов'язковими заходами**: `(1) зібрати докази, `(2) підтвердити вибір offline, обмеженим списком кандидатів`.

### 6.1 Режим 1 — `discovery` (мережа)

```bash
python scripts/resolve_articles.py \
  --topic "intermittent fasting" \
  --projects en.wikipedia --projects pl.wikipedia \
  --topic-for pl.wikipedia="głodówka" \
  --out out/resolved.json --ttl-hours 24
```

Алгоритм по кроках (послідовно, в порядку `--projects`, ~1 запит/с):

| Крок | Запит | Мета |
|---|---|---|
| 1 | `action=query&list=search&srsearch=<query>&srnamespace=0&srlimit=5&srsort=relevance` | до 5 кандидатів, тільки namespace 0 |
| 2 | `action=query&redirects=1&titles=<запит>\|<hit1>\|…&prop=info\|pageprops&ppprop=disambiguation` | один батч: канонічні назви, редіректи, прапорці disambiguation |
| 3 | AQS per-article, 30 днів | чи є у статті хоч якийсь обсяг |
| — | *(пропускається)* | якщо пошук дав 0 хітів: проєкт одразу `unresolved` |

Ключові деталі:

- **Без `srwhat`** — свідомо не звужується пошук (`what=text` дав би гірші результати).
- **Роздільник батча — літеральний `|`**, потім `urlencode` всього URL.
- **`follow_redirects`** простежує ланцюг до 10 хопів; цикл → `redirect_cycle`, глибина → `redirect_too_deep`.
- **`exact_title_match`** = `true` лише коли нормалізоване ім'я запиту **точно** дорівнює фінальній назві **і** ланцюг редіректів порожній. Ранг пошуку ≠ підтвердження.

### 6.2 Статуси кандидата

| Причина відмови | Код | Що це означає |
|---|---|---|
| сторінка відсутня | `missing_target` | title не існує |
| не стаття | `non_article_namespace` | категорія, шаблон, Portal: тощо |
| сторінка розмежування | `disambiguation_page` | не можна виміряти — це список значень |
| петля редіректів | `redirect_cycle` | A→B→A |
| надто глибокий ланцюг | `redirect_too_deep` | > 10 хопів |

Кандидат зі статусом `unresolved` **зберігається з доказом** (`input_titles`, `redirect_chain`, `namespace`) — він не видаляється. Це важливо: у `resolved.json` видно, *чому* певна стаття не підійшла, і модель може це переказати користувачеві.

### 6.3 Доказ обсягу (volume probe)

Для кожного `selectable` кандидата робиться один AQS-запит за останні 30 повних UTC-днів:

```
https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/{project}/all-access/user/{article}/daily/{start}00/{end}00
```

Валідація відповіді **повна**: кожен item повинен точно збігатися за `project`, `article` (percent-encoded), `access="all-access"`, `agent="user"`, `granularity="daily"`; `timestamp` — рівно 10 цифр з `00`; дата в межах вікна; без дублікатів; `views` — невід'ємне ціле. Перевірка відбувається **до** підсумовування, до запису в кеш і до мутації `candidate.volume`.

Результат:

```json
{ "status": "available", "total_views": 23635,
  "window": {"start": "2026-08-27", "end": "2026-09-25"},
  "observed_days": 30, "last_observed_date": "2026-09-25",
  "low_volume": false, "reason": null }
```

```json
{ "status": "unavailable", "total_views": null,
  "window": {"start": "2026-08-27", "end": "2026-09-25"},
  "observed_days": null, "last_observed_date": null,
  "low_volume": null, "reason": "aqs_404_zero_or_not_loaded" }
```

**Правило замість нуля.** `unavailable` — це `null`, ніколи не `0`. Причина одна й та сама, що й в `metrics.json`: нуль читається як вимірювання («виміряли — вийшло нуль»), а фактично «виміряти було неможливо». `low_volume` — це **лише мітка на доказі** (`total_views < 1000`); вона не змінює статус кандидата й не змінює код виходу. Стаття з 40 переглядами за місяць залишається `selectable` — агент не забороняє її вимірювати, він лише позначає.

### 6.4 Статус проєкту й рекомендація

```python
selectable = [c for c in candidates if c["status"] == "selectable"]
status = "ready"       if len(selectable) == 1
        else "ambiguous" if selectable
        else "unresolved"
```

`recommendation` — це рядок (або `null`), а не вибір. Правило: якщо `selectable` рівно один → повернути його `article`; якщо кілька, але рівно один має `exact_title_match is True` → повернути його; інакше `null`.

Агрегований статус (перший збіг):

| Умова | `status` | Exit |
|---|---|---|
| усі проєкти `error` | `error` | 1 |
| є хоч один `unresolved` | `unresolved` | 2 |
| є `error` **і** є `ready`/`ambiguous` | `partial_error` | 3 |
| інакше | `awaiting_confirmation` | 0 |

`unresolved` домінує над `partial_error`: невирішена стаття важливіша за часткову помилку.

### 6.5 Режим 2 — `confirm` (offline, обов'язковий)

```bash
python scripts/resolve_articles.py \
  --topic "intermittent fasting" \
  --projects en.wikipedia --projects pl.wikipedia \
  --topic-for pl.wikipedia="głodówka" \
  --out out/resolved.json \
  --select en.wikipedia=Intermittent_fasting \
  --select pl.wikipedia=G%C5%82od%C3%B3wka_lecznicza
```

`confirm` **не робить жодного мережевого запиту**. Наявність `--select` і перемикає режим — автовиявлення немає.

Умови, які перевіряються (усі разом, одним списком):

1. збережений документ існує, валідний UTF-8 JSON, об'єкт;
2. `contract_version == "resolved.v1"`, `run_mode == "discover"`, `status == "awaiting_confirmation"`;
3. `topic`, `topic_overrides` і **порядок** `projects` відтворено **точно**;
4. набір `selection` дорівнює набору запитаних проєктів (не менше, не більше);
5. кожен вибраний артикл — **рівно один** збережений `selectable` кандидат;
6. для `ambiguous`-проєкту обов'язковий `--reason`.

### 6.6 Правило candidate-bounded

```python
matches = [c for c in candidates
           if c.get("status") == "selectable"
           and c.get("article") == selected_article]
if len(matches) != 1:
    errors.append(f"selection is not one exact saved candidate: {code}={selected_article}")
```

**Наслідок:** підтвердити можна лише те, що discovery вже запропонував. Слаг, який модель «пам'ятає» (`Post_przerywany`), буде відхилено, а не тихо створено. Відмова:

```
resolver confirmation failed:
- selection is not one exact saved candidate: pl.wikipedia=Post_przerywany
```

exit `2`, а **попередні байти discovery-манифесту на диску залишаються недоторканими** — трансформація відбувається тільки після перевірки помилок.

Це робить другий запуск таким самим безпечним, як перший: підтвердити можна лише те, що можна побачити в доказах.

### 6.7 Структура `resolved.json`

Рівень root — рівно 8 ключів:

```json
{
  "contract_version": "resolved.v1",
  "run_mode": "discover",
  "status": "awaiting_confirmation",
  "topic": "intermittent fasting",
  "topic_overrides": [ { "project": "pl.wikipedia", "query": "głodówka" } ],
  "generated_at": "2026-09-26T09:15:22.918273+00:00",
  "volume_window": { "start": "2026-08-27", "end": "2026-09-25", "days": 30,
                     "access": "all-access", "agent": "user", "granularity": "daily" },
  "projects": [ /* рівно 11 ключів кожен */ ]
}
```

Проєкт (11 ключів): `project`, `effective_query`, `query_source` (`shared`|`project_override`), `status` (`ready`|`ambiguous`|`unresolved`|`error`), `recommendation`, `search_hits`, `candidates`, `selection`, `reason` (`search_no_hits`|`no_selectable_candidates`|`null`), `error` (`{code, message}`|`null`).

`search_hits[i].rank` **зобов'язаний** дорівнювати своїй позиції (`rank == index + 1`) — це захист від підробки «релевантності», яку неможливо перевірити інакше.

### 6.8 Валідація входу

Усі порушення збираються **разом**, до будь-якої роботи з мережею:

```
resolver input validation failed:
- project is not in the committed allowlist: xx.wikipedia
- duplicate project: pl.wikipedia
- select project is not requested: cs.wikipedia
```

Обмеження: максимум **8** проєктів, максимум **5** пошукових хітів, максимум **10** редірект-хопів, відповідь ≤ **1 MiB**, рядки ≤ **256** code points без C0/C1-керуючих символів, значення `--select`/`--topic-for` не можуть бути URL. Каталог проєктів — `assets/wikipedia-projects.json` (`wikipedia-projects.v1`), у якому зберігаються **stems** (`en.wikipedia`), а `.org` додається лише в одному місці шаблоном:

```python
if project not in load_allowed_projects():
    raise ResolveInputError(...)
return f"https://{project}.org/w/api.php?{urlencode(params)}"
```

Значення користувача фізично не може внести scheme, port або path.

### 6.9 Percent-encoding

```python
def canonical_article(title: str) -> str:
    return quote(title.replace(" ", "_"), safe="")
```

Пробіли → `_`, далі кодується все, що не є unreserved. **Без Unicode-нормалізації, без case folding** — беруться канонічні назви з Action API. Результат іде в: `candidate.article`, URL AQS, валідацію item'ів AQS, ключ кешу, групування дублікатів, значення `--select` і, врешті, **`spec.series[].article`**.

---

## 7. Стадія fetch: `fetch_pageviews.py`

### 7.1 Обмеження вікна

```python
def effective_end(spec_end_ymd: str, yesterday_utc: date) -> str:
    return min(_parse_ymd(spec_end_ymd), yesterday_utc).strftime("%Y%m%d")
```

Кінець вікна притискається до **останнього повного UTC-дня**. Запит за майбутнім або сьогоднішнім днем не дає нульових днів у CSV — вони просто не існують.

### 7.2 Chunk-и

```python
day_count = min(365, (end - chunk_start).days + 1)
```

Вікно ділиться на безперервні chunk'и по ≤ 365 днів. Ліміт 365 — це переважно ввічливість (сервер приймає довші вікна), але також рішення про коректність: довге вікно, що впало на половині, означає довгий retry.

### 7.3 Таксономія 404

```python
def classify_404(window_end_ymd: str, yesterday_utc: date) -> str:
    gap = (yesterday_utc - _parse_ymd(window_end_ymd)).days
    return "not_loaded" if gap <= NOT_LOADED_GAP_DAYS else "no_views"
```

| Відстань | Класифікація | Що пишеться |
|---|---|---|
| ≤ 2 дні | `not_loaded` | **нічого** — даних ще немає, повторити пізніше |
| > 2 дні | `no_views` | по одному чесному `0` на кожен день chunk'а |

Це найважливіше рішення у всій стадії fetch. Wikimedia публікує дані з **затримкою ~2 доби**. Наивний клієль перетворив би «ще не опубліковано» на «нуль переглядів» — тобто вирішив би, що інтерес нульовий. Стан `not_loaded` і стан `no_views` **ніколи не об'єднуються**.

За `quality.fail_on_empty_series = true` серія, у якої всі chunk'и повернули `not_loaded`, робить запуск fatal (exit 1) — після повного сканування, а не мовчки.

### 7.4 Вихідний CSV

```
date,views,series_id,project,article
2024-09-23,2087,pl-post-przerywany,pl.wikipedia,G%C5%82od%C3%B3wka_lecznicza
```

Рядки сортуються за `(series_id, date)`. Відсутні дні **не** доповнюються нулями на цьому етапі — вони залишаються відсутніми, і `make_charts.py` малює їх як розрив (див. §9.1). Це принципово: нуль у CSV означав би вимірювання.

### 7.5 Мережева політика

| Аспект | Правило |
|---|---|
| Throttle | ~1 запит/с, серіально в процесі |
| User-Agent | обов'язковий, fail-fast (§12.1) |
| 403 | **негайно fatal**, не повторюється: `HTTP 403 — set a real WTI_USER_AGENT contact and retry` |
| 429 / 5xx | до 3 спроб, `Retry-After` (секунди або HTTP-date), fallback **5.0 с** |
| інший non-200 | помилка цього запиту, не retry |
| Timeout | 30 с на запит |
| `transport` | ін'єктований — у тестах підміняється, мережа не потрібна |

Різниця між 429 і 403 не косметична: **429 означає «сповільнись», 403 — «виправ ідентифікацію»**. Retry-цикл може виправити перше і ніколи не друге.

### 7.6 Кеш

- Розташування: `.cache/` (змінна `WTI_CACHE`).
- Ключ: **SHA-256 повного URL**, файл `<key>.json`.
- Конверт: рівно `{ "fetched_at": ..., "status": 200, "response": ... }`.
- Пишеться **тільки перевірений HTTP 200**. Конверт, що не пройшов валідацію (не той status, немає `fetched_at`, `items` не список), **видаляється**, а запит повторюється.
- TTL: `WTI_TTL_HOURS` (за замовчуванням 24). `--ttl-hours 0` вимикає читання, але не змінює ключ і конверт.
- Скидання: `rm -rf .cache`. Іншого механізму інвалідації немає, і на цій масштабі він не потрібен.

Кеш — головна причина, чому повторне або уточнювальне питання коштує нульових запитів. У зведенні fetch видно `cache hits: 4/6`.

---

## 8. Стадія analyze: `analyze_trends.py`

Це ядро аналітики. Розбір — із формулами.

### 8.1 Вікна зростання

```python
GROWTH_WINDOWS = {"m3": 91, "y1": 365, "y2": 730}
```

Три вікна. `YTD` у v1 немає, і налаштування через CLI теж немає — усі константи модульні, а не прапорці.

Межі вікон:

```python
current_start  = end_date - timedelta(days=days - 1)   # включно рівно `days` днів
previous_end   = current_start - timedelta(days=1)
previous_start = previous_end - timedelta(days=days - 1)
```

`end_date` — **остання спостережена дата самої серії**, не кінець вікна зі spec. Вікна безперервні, однакової довжини, не перетинаються.

### 8.2 Формула

```python
previous_mean = Σ previous_values / len(previous_values)     # точний Fraction
current_mean  = Σ current_values  / len(current_values)      # точний Fraction
exact_pct = (current_mean / previous_mean - 1) * 100
exact_abs = current_mean - previous_mean
```

Округлення — **один раз, на межі серіалізації**:

```python
"pct": float(round(exact_pct, 1)),
"abs": int(round(exact_abs)),
```

Середнє рахується на `Fraction`, тому `+16.7%` — це не «приблизно 16.67, округлили», а число без помилки округлення на вході.

### 8.3 Поріг покриття

```python
floor = math.ceil(COVERAGE_RATIO * days)     # COVERAGE_RATIO = 0.8
```

| Вікно | Днів | Мінімум спостережень |
|---|---|---|
| `m3` | 91 | 73 |
| `y1` | 365 | 292 |
| `y2` | 730 | 584 |

Поріг застосовується до **обох** вікон. Причина: `y2` вимагає 730 днів поточного вікна **плюс** 730 днів базового, тобто реально 1460 днів даних. Тому у прикладі на 728 днів `2Y` завжди `н/д` — і це правильно, а не баг.

### 8.4 Варіант `clean` (без спайків)

```python
def replace_anomalies(observations, anomalies):
    replacements = {a["date"]: a["median"] for a in anomalies}
    return [Observation(..., views=replacements.get(o.date.isoformat(), o.views), ...)
            for o in observations]
```

Аномалії **замінюються медіаною**, а дати зберігаються. Тому набір дат, довжини вікон, межі та пороги покриття для raw і clean **ідентичні** — різниця лише в самих значеннях.

Структура результату:

```json
"y1": {
  "pct": 19.6,
  "abs": 412,
  "start": "2025-09-21",
  "end":   "2026-09-20",
  "clean": { "pct": 16.7, "abs": 364 }
}
```

- `pct` / `abs` — **raw** (з аномаліями);
- `clean.pct` / `clean.abs` — те саме на series без аномалій;
- `reason` з'являється **тільки** коли `pct is null` (і дублюється всередині `clean`).

**Правило відповіді:** завжди цитувати `clean`, ніколи `raw`. Друкувати обидва — значить зробити виключену аномалію частиною висновку.

### 8.5 Детекція аномалій

```python
MAD_RADIUS_DAYS = 3      # ±3 дні → ефективне вікно 7 днів
MAD_SCALE = 1.4826       # коефіцієнт нормальної узгодженості
MAD_K = 3.5              # поріг
MIN_SERIES_OBSERVATIONS = 14
MIN_LOCAL_OBSERVATIONS = 3
```

```python
for observation in observations:
    local = [v for c in observations
             if abs((c.date - observation.date).days) <= MAD_RADIUS_DAYS]
    if len(local) < MIN_LOCAL_OBSERVATIONS:
        continue
    local_median = median(local)
    raw_mad = median(abs(v - local_median) for v in local)
    if raw_mad == 0:
        continue
    score = (observation.views - local_median) / (raw_mad * MAD_SCALE)
    if abs(score) >= MAD_K:
        found.append({"date": ..., "value": ..., "median": local_median})
```

Тонкощі, які варто знати:

- Вікно **центроване в часі календаря**, а не за індексами рядків: пропуск у даних зменшує ефективну вибірку. Reindexing і заповнення нулями **не** робляться.
- Точка **входить у власне вікно**.
- `raw_mad == 0` (плоске вікно) → аномалія неможлива, точка пропускається.
- Спайк і провал **не розрізняються**: запис просто має `value < median`.
- Виявлення виконується **перед** обчисленням зростання — саме тому `clean` існує для кожного вікна.

Запис аномалії:

```json
{ "date": "2026-03-14", "value": 5120, "median": 310 }
```

Похідна величина: `anomaly_share = len(anomalies) / len(observations)`.

### 8.6 Сезонність

Сезонність рахується **тільки** за наявності двох вирівняних 365-дневних напівперіодів:

```python
previous = (end - 729, end - 365)
current  = (end - 364, end)
```

Якщо в **хоча б одній** половині якийсь із 12 місяців не має жодного рядка — результат `unavailable` (`None`). Частково ідентифікований фіт не намагаються.

Модель: квадратичний тренд у кожній половині + спільні місячні ефекти з нульовою сумою.

```
y = a_h + b_h·x + c_h·x² + s_month        h ∈ {previous, current}
s_12 = -(s_1 + … + s_11)
```

Разом **17 вільних параметрів** (3+3+11). Нормальні рівняння збираються з достатніх статистик (`rows, x, x², x³, x⁴, y, xy, x²y`) і розв'язуються **методом Гауса з вибором провідного елемента над `Fraction`**. Нульовий pivot → `singular non-identifiable normal-equation system: zero pivot at column {c}`.

Місяць повідомляється, коли:

```python
shared_effects[month] > 0
and all(pre_effect_residual[month][half] - shared_effects[month] >= 0
        for half in range(2))
```

Тобто ефект має бути додатнім **і** після його віднімання залишок у **обох** половинах не від'ємний. Місяць, виміряний лише в одному з двох спостережених років, не повідомляється як повторюваний.

Вихід:

```json
{ "months": [1, 3, 12], "note": "recurring peaks have positive shared month effects whose ... " }
```

`note` навмисно довгий і має цитуватися дослівно, а не переказуватись як «сезонне / не сезонне».

### 8.7 Рубрика `confidence`

```python
score_confidence(period_days, monthly_30d, anomaly_share,
                 clean_y1_available, comparison_span) -> (level, reasons)
```

| Терм | Умова | Бали | Reason (дослівно) |
|---|---|---|---|
| Період | `≥ 730` днів | **+2** | `period at least 730 days` |
| Період | `91…729` днів | **+1** | `period at least 91 days` |
| Період | `< 91` дня | +0 | `period below 91 days` |
| Обсяг | 30-дн. середнє `≥ 10000` | **+2** | `monthly 30-day views at least 10000` |
| Обсяг | `1000…10000` | **+1** | `monthly 30-day views at least 1000` |
| Обсяг | `< 1000` | +0 | `monthly 30-day views below 1000` |
| Аномалії | `anomaly_share == 0` | **+1** | `no anomalies detected` |
| Аномалії | `≤ 0.05` | +0 | `anomaly share within 5 percent` |
| Аномалії | `> 0.05` | +0 | `anomaly share above 5 percent` |
| Методологія | вікно перетинає `2015-05-01` | **−2** | `comparison crosses 2015-05-01 methodology break` |
| Розкриття | `clean` 1Y недоступний | +0 | `clean 1-year growth unavailable` |
| Розкриття | рівень `low` | +0 | `low confidence: treat the reading as a hypothesis` |

Смуги: `score ≥ 4` → `high`; `2 ≤ score < 4` → `medium`; інакше `low`.

Три правила, які не можна порушити при інтерпретації:

1. `clean 1-year growth unavailable` **не коштує балів**. Це розкриття, а не штраф. Треба сказати про це вголос, а не мовчки відкатитися на raw.
2. **Не відновлювати рівень за кількістю reasons.** Список reasons може бути довшим за кількість термів (розкриття додає reason без терма; вердикт `low` додає ще один). Треба або рахувати бали, або цитувати виданий рівень.
3. **Штраф за 2015 — єдиний від'ємний терм.** Двохрідна, високообсягова серія без аномалій може бути `low` **виключно** через перетин 2015. Це треба сказати прямо, а не подавати як «слабкі дані».

`monthly_30d` порівнюється як **точний `Fraction`** (`Fraction(total, period_days) * 30`), а не як float.

### 8.8 `trend_direction`

```python
DIRECTION_BAND_PCT = 10.0

def _direction_band(pct):
    if pct > 10.0:  return "up"
    if pct < -10.0: return "down"
    return "flat"
```

```python
if period_days < 91 or clean_y1_pct is None:  return "inconclusive"
if confidence == "low" or monthly_30d < 1000: return "noise"
if raw_y1_pct is None:                        return "inconclusive"
raw_band, clean_band = _direction_band(raw_y1_pct), _direction_band(clean_y1_pct)
return clean_band if raw_band == clean_band else "noise"
```

П'ять значень: `up`, `down`, `flat`, `noise`, `inconclusive`. Мертва зона ±10% входить у `flat` (порівняння строге: рівно `+10.0` → `flat`).

Ключова семантика:

- **`up` ≠ «інтерес зростає».** `up` означає: raw і clean збігаються за напрямом **і** пройдені всі гейти. Це твердження про два вікна, а не про світ.
- **`inconclusive` і `noise` — це відповіді, а не збої.** Їх треба повідомити як знахідки. Мовчки викинути серію або повідомити raw-відсоток як напрям — порушення.
- Якщо один спайк здатний перевернути знак, `safe_direction` повертає `noise`. Саме для цього існує `clean`.

### 8.9 Структура `metrics.json`

Рівень root — рівно 4 ключі:

| Ключ | Тип | Значення |
|---|---|---|
| `spec_name` | string | `spec["name"]` дослівно |
| `as_of` | `YYYY-MM-DD` | **глобальний** max-date по всіх серіях |
| `generated_from` | string | аргумент `--spec` дослівно |
| `series` | array | по одному елементу на серію, **в порядку `spec.series`** |

Кросс-серійних агрегатів немає свідомо: будь-яке таке число було б другою похідною (D-03).

15 ключів на серію:

| Ключ | Приклад |
|---|---|
| `series_id` | `"pl-post-przerywany"` |
| `project` | `"pl.wikipedia"` |
| `article` | `"G%C5%82od%C3%B3wka_lecznicza"` |
| `label` | `"Польська: голодування загалом"` |
| `language` | `"pl"` |
| `total_views` | `5814` |
| `avg_daily_views` | `8.0` |
| `period` | `{"start": "2024-09-23", "end": "2026-09-20", "days": 728}` |
| `growth` | `{"m3": …, "y1": …, "y2": …}` |
| `anomaly_share` | `0.017881705639614855` |
| `trend_direction` | `"noise"` |
| `confidence` | `"low"` |
| `confidence_reasons` | `["period at least 91 days", …]` |
| `seasonality` | `{"months": [], "note": "…"}` |
| `anomalies` | `[{"date": …, "value": …, "median": …}]` |

`period` будується з **першої та останньої спостереженої** дати, а не з меж spec.

### 8.10 Правило `null` і захист від помилок

`pct: null` означає **not computable** і завжди має `reason` поруч. Ніколи не `0`. Причини рівно дві:

| Reason | Значення |
|---|---|
| `insufficient observations in one or both equal-length windows` | бракує спостережень в одному або обох однакових вікнах |
| `previous equal-length window has zero mean` | середнє базового вікна рівно нулю, відсоток математично не визначено |

Перед серіалізацією `validate_finite_numbers` рекурсивно відкидає нефінітні числа, потім виконується контрольний `json.dumps(..., allow_nan=False)`.

CSV also validated: заголовок точно `date,views,series_id,project,article`, ≤ 16 MiB, ≤ 50 000 рядків, кожен `series_id` є в spec, `project`/`article` збігаються, дата в межах вікна, дублікатів дат немає, `views` — невід'ємне ціле ≤ 20 цифр.

---

## 9. Стадії charts і report

### 9.1 `make_charts.py` — інвентар 2N+1

Для N серій генерується рівно `2N + 1` зображень:

| # | Файл | Тип | Що показує |
|---|---|---|---|
| 1..N | `chart_<id>_timeseries.png` | часовий ряд | raw-день + ковзна медіана за 7 днів + аномалії + розриви |
| N+1..2N | `chart_<id>_growth.png` | горизонтальні бари | `clean.pct` для `3M` / `1Y` / `2Y` |
| 2N+1 | `chart_overlay.png` | накладка | усі серії на одній сирій осі |

Порядок: для кожної серії timeseries → growth підряд, накладка — **остання**. Її ім'я — константа, не походить від `series_id`, тому не може зіткнутися з персональним графіком.

**Чому саме так.** Накладка не нормалізує і не ребазує лінії: вона показує **сирі** величини, щоб читач сам побачив, чи масштаби збігаються. На зображенні пишеться `scales differ`, а в `charts.json` — ASCII-константа `comparative view; per-series scales differ`.

**Розриви в даних** — не нулі. Кожен відсутній календарний день вставляється як `float("nan")`, matplotlib розриває лінію без інтерполяції, плюс смуга `axvspan` з підписом діапазону (`2026-03-01..2026-03-04`). Нуль у CSV читався б як вимірювання.

**Аномалії** малюються вертикальними відрізками `#E8590C` від `median` до `value`. Якщо `value == median`, додається точний маркер. Значення беруться з `metrics.json` дослівно і ніколи не переобчислюються.

**`null`-бари** на графіку зростання — це штриховані сірі заглушки шириною 2% від розмаху, з підписом `н/д` і локалізованою причиною. Порожня клітинка або нуль були б брехнею.

**Лінійна вісь** має підлогу рівно `0.0` (D-14) — інакше matplotlib зробив би від'ємні значення переглядів. У режимі `--log-scale` підлогою стає **найменше строго додатне** значення, недодатні дні маскуються (`nonpositive="mask"`), їхня кількість публікується в `log_masked_points`, а на зображенні пишеться `log scale; 4 non-positive day(s) masked and not drawn`.

Автоматичного перемикання на log **немає** — це явний прапорець. Великий розмах візиє стислює решту, але нічого не видаляє й кожне значення публікується в `charts.json`.

`charts.json` (`charts.v1`) — 7 ключів у root: `contract_version`, `spec_name`, `as_of`, `language`, `generated_from`, **`metrics_sha256`**, `charts`. На кожен запис — 11 обов'язкових ключів (`kind`, `label`, `language`, `filename`, `yscale`, `log_masked_points`, `y_limits`, `points`, `gaps`, `anomalies_drawn`, `subtitle`) плюс умовні (`log_note`, `x_limits`, `series_id`, `spec_index`, `series_ids`, `bars`, `note`) — умовні **відсутні**, а не `null`.

### 9.2 `build_report.py` — сім розділів

Порядок розділів — заморожений контракт `SECTION_TOKEN_KEYS`:

| # | Заголовок (uk) | Вміст |
|---|---|---|
| 1 | `# Аналіз переглядів Wikipedia` | H1 |
| 2 | `## Висновок` | по булеті на пару (серія × вікно): `3M +9.1% · на основі 8.0 Переглядів/день` |
| 3 | `## Метрики` | `as_of` + таблиця GFM на 11 колонок |
| 4 | `## Наскільки можна довіряти` | рівень + причини; префікс `гіпотеза: ` лише при `low` |
| 5 | `## Графіки` | 2N+1 зображень відносними шляхами |
| 6 | `## Обмеження та припущення` | `assumptions` + seasonality note + розкриття по кожному графіку |
| 7 | `## Наступний крок` | один рядок: `Це не прогноз і не рекомендація.` |
| — | `---` + футер | джерело, `as_of`, `Виміряно` |

### 9.3 Реальний звіт (live-run, 2026-09-27)

```markdown
# Аналіз переглядів Wikipedia

## Висновок

- Польська: голодування загалом (найближча наявна стаття, не інтервальне) · 3M вікно: +9.1% · на основі 8.0 Переглядів/день
- Польська: голодування загалом (найближча наявна стаття, не інтервальне) · 1Y вікно: -38.5% · на основі 8.0 Переглядів/день
- Польська: голодування загалом (найближча наявна стаття, не інтервальне) · 2Y вікно: н/д (недостатньо спостережень в одному або в обох однакових за довжиною вікнах) · на основі 8.0 Переглядів/день
- Чеська: інтервальне голодування · 3M вікно: -24.4% · на основі 9.2 Переглядів/день
- Чеська: інтервальне голодування · 1Y вікно: -44.5% · на основі 9.2 Переглядів/день
- Чеська: інтервальне голодування · 2Y вікно: н/д (недостатньо спостережень в одному або в обох однакових за довжиною вікнах) · на основі 9.2 Переглядів/день

## Метрики

Дані станом на 2026-09-20

| Тема | Проєкт | Стаття | Мова | Період | Усього переглядів | Переглядів/день | Зростання | Напрям | Впевненість | Частка аномалій |
|---|---|---|---|---|---|---|---|---|---|---|
| Польська: голодування загалом (найближча наявна стаття, не інтервальне) | pl.wikipedia | G%C5%82od%C3%B3wka_lecznicza | pl | 2024-09-23..2026-09-20 (728) | 5,814 | 8.0 | 3M +9.1% / 1Y -38.5% / 2Y н/д (…) | шум | низька | 0.017881705639614855 |
| Чеська: інтервальне голодування | cs.wikipedia | P%C5%99eru%C5%A1ovan%C3%BD_p%C5%AFst | cs | 2024-09-23..2026-09-20 (728) | 6,708 | 9.2 | 3M -24.4% / 1Y -44.5% / 2Y н/д (…) | шум | низька | 0.03310344827586207 |

## Наскільки можна довіряти

- Польська: голодування загалом (найближча наявна стаття, не інтервальне): гіпотеза: низька — період щонайменше 91 день; переглядів за 30 днів менше 1000; частка аномалій у межах 5 відсотків; низька впевненість: трактуйте значення як гіпотезу
- Чеська: інтервальне голодування: гіпотеза: низька — період щонайменше 91 день; переглядів за 30 днів менше 1000; частка аномалій у межах 5 відсотків; низька впевненість: трактуйте значення як гіпотезу

## Наступний крок

Це не прогноз і не рекомендація.
```

**Як це читати.** Обидві серії мають `low` — і це **правильна відповідь**. При 8.0 і 9.2 переглядів/день це приблизно 215 і 248 на місяць, тобто далеко нижче порогу шуму 1000. `-38.5%` і `-44.5%` — це те, що робить серія з 3 перегляди на день, коли один популярний тиждень перетинає межу вікна. Звіт їх показує, **щоб читач бачив, чому їх не можна цитувати**.

Другий момент, який видно з цього виводу: `assumptions` кажуть, що дві серії **вимірюють різні теми**. `cs.wikipedia/Přerušovaný půst` — стаття саме про інтервальне голодування; у `pl.wikipedia` такої статті немає взагалі, і найближча `Głodówka lecznicza` описує голодування загалом. Агент виміряв суміжні теми — і сказав про це вголос, замість підмінити статтю на ту, що дала б красивіші числа.

### 9.4 Форматування чисел

Один шов (`format_number`):

```python
def format_number(value, *, percent=False):
    return f"{value:+.1f}" if percent else f"{value:,}"
```

- відсотки: завжди зі знаком, завжди 1 знак: `+16.7%`, `-22.0%`, `+0.0%`;
- числа: кома як роздільник тисяч, без локалі (`locale` заборонено — AST-тест падає на самому імпорті);
- `null` → локалізований токен + причина: `н/д (період коротший за 91 день)`;
- `md_cell` екранує `|` і переноси рядків у таблиці.

Відома особливість v1: `anomaly_share` друкується **без** знака відсотка (`0.0372`), бо це сира частка 0..1. Зафіксовано в реєстрі дефектів.

### 9.5 Мови: чому chart приймає більше, ніж report

| Стадія | Мови | Поведінка при відсутній мові |
|---|---|---|
| `make_charts.py` | `en`, `uk`, `ja` | `ChartError: no chart tokens for language: {lang}` |
| `build_report.py` | `en`, `uk` | `ReportError: no report tokens for language: {lang}` → exit `1` |

Прийнятий набір — це **перетин** двох таблиць, а не об'єднання. Тому spec з `language: "ja"` дає **валідний `charts.json`**, а потім відмову на стадії report з exit `1` і **без написаного файлу**.

Це навмисно, і саме тому `run_all.py` **не робить** успішну стадію charts передумовою report: інакше модель прочитала б «графіки побудовано» як «запуск вдався».

Обидві таблиці **fail-closed**: частково заповнена таблиця = відмова, а не відкат на англійську. Немає silent fallback.

Ще одне обмеження, задокументове як v1-gap: шрифт DejaVu покриває Latin і Cyrillic. CJK рендериться як tofu-квадрати з одним попередженням у stderr. Fallback-шрифтів у v1 немає.

### 9.6 Ланцюг цілісності

`report.manifest.json` (`report.v1`) — 10 ключів, зокрема:

```json
{
  "contract_version": "report.v1",
  "metrics_sha256": "…",
  "charts_sha256": "…",
  "report_filename": "report.md",
  "metrics_shown": ["series[0].growth.y1.clean.pct", "series[0].avg_daily_views", …],
  "formats": [ { "format": "markdown", "filename": "report.md" } ]
}
```

`metrics_shown` — вказівники з правилом **first-occurrence-wins**: набір без дублікатів, тому порівняння множин не залежить від порядку.

`formats` — масив **об'єктів**, а не рядків. Це готовий слот для PDF у v2: один доданий об'єкт і один файл, а не перейменування Markdown-слота.

`report_is_stale(out_dir)` повертає `True` на **кожному** невідповідному стані (відсутній/битий маніфест, відсутній `report.md`, розбіжність будь-якого з двох SHA-256) і `False` лише коли обидва дайджести збігаються з байтами на диску. Механізм цілісності, не автентифікації.

---

## 10. Чому цим можна довіряти

### 10.1 Три правила чесності

1. **`pct: null` означає *not computable*, ніколи `0`.** Поруч завжди є `reason`. Читач, що бачить `0`, вирішує, що значення виміряли й воно нульове. Насправді його **виміряти було неможливо**.
2. **Відсоток без бази обсягу — не число.** `+5.7%` на 2363.5 переглядів/день і `+5.7%` на 13 переглядів/день — різні факти в однаковій обгортці, і лише один виживає поріг шуму.
3. **Інтерес до статті — не попит на продукт.** Нічого тут не прогнозує; кожне число описує вже завершені дні.

### 10.2 П'ять підводних каменів (за `DATA_CAVEATS.md`)

| # | Камінь | Як проявляється | Чого не можна concludes |
|---|---|---|---|
| 1 | **Розрив методології 2015-05-01** | аналізатор виявляє перетин і віднімає **2** бали | що «до 2015 не тренд» — вікно 2024–2026 не зачеплене; якщо перетин є, казати про це **в тому ж реченні**, що й відсоток |
| 2 | **Поріг шуму 1000/30 днів** | `trend_direction` примусово `noise` | що `+400%` на 9 переглядів/день — арифметичний артефакт малого знаменника |
| 3 | **Спайк ≠ зростання** | аномалії виявляються до вікон; кожне вікно має `clean` | що спайк частина тренду — цитувати `clean` |
| 4 | **`unavailable`/`null` ≠ нуль** | 404 → `null`, не `0`; некоректне вікно → `pct: null` | що «0» і «не можна обчислити» — близькі; це різні твердження, і лише одне з них є фактом |
| 5 | **Шрифт CJK** | tofu-квадрати + warning у stderr; chart приймає більше мов, ніж report | що успішний chart = успішний запуск |

### 10.3 Межа, яку не можна переходити ніде

> **Інтерес до статті — це не попит на продукт і не здатність платити.**

Це має бути сказано у висновках. Агент вимірює увагу до статті Wikipedia за вікно, яке вже минуло. `trend_direction: up` — це твердження про порівняння двох вікон, а не прогноз.

### 10.4 Приклад формулювань

| ❌ Не писати | ✅ Писати |
|---|---|
| «Інтерес до інтервального голодування зростає на 5,7%» | «За чистим 1Y інтерес у польській Вікіпедії +5,7% на базі 2363,5 переглядів/день; впевненість низька, тож це гіпотеза, а не висновок» |
| «Тема втрачає популярність» | «За чистим 1Y інтерес у чеській Вікіпедії −3,1% на базі 412,0 переглядів/день; впевненість низька через 8% аномалій — напрям не стійкий» |
| «Дані ненадійні, краще не робити висновків» | «Впевненість низька: період 60 днів коротший за 91 день, переглядів за 30 днів 210, нижче порогу 1000. Це твердження про коротке вікно, а не тренд» |

Ці пари BEFORe/AFTER живуть у `references/INTERPRETATION.md` і цитуються моделі дослівно. Вони **обов'язкові**, а не стилістична порада.

### 10.5 Обмеження, які агент визнає сам

- Сезонність вимагає **двох** вирівняних 365-дневних напівперіодів → фактично ≥ 2 років даних. На 728 днях вона недоступна.
- Два спостережені цикли не дозволяють відокремити сезонний ефект від календарної кривизни.
- Аномалії не розрізняють спайк і провал.
- Сирі лінії на накладці не нормалізовані — масштаби можуть відрізнятися (про це написане на самому зображенні).
- Pageviews вимірюють **відвідування**, а не унікальних читачів.
- RedIRECT-и враховуються по-різному для редіректів і цільових статей.

---

## 11. Якість, тести й оцінка дешевої моделі

### 11.1 Тести

14 файлів (13 `test_*` + `conftest.py`), 25 фікстур. **Жодного мережевого виклику** — це частина контракту, а не збіжність.

| Файл | Що перевіряє |
|---|---|
| `test_analyze.py` | зростання на crafted-series, інжекція спайка/провалу, сезонність, межі рубрики, `None` vs `0` |
| `test_fetch.py` | cache hit/miss, 429→`Retry-After`, 403, 404→порожня серія, timeout, TTL, `--ttl-hours` |
| `test_charts.py` | PNG, існує, ненульовий розмір, PNG magic bytes; AST-перевірки (немає `import locale`, немає нульової підлоги на осі зростання) |
| `test_report.py` | кожне число з `metrics.json` присутнє у `report.md`; вирівнювання таблиці; жодного `None`/`nan` у звіті |
| `test_contracts.py` | заморожені JSON-контракти |
| `test_packaging.py` | `name` == назва директорії; рядки з `INTERPRETATION.md` збігаються з константами коду (імпортом, не копіюванням) |
| `test_resolve.py` | обидва режими резолвера, candidate-bounded, агрегація статусів |
| `test_run_all.py` | оркестрація, коди виходу, зупинка на першій помилці |
| `test_eval.py`, `test_validate.py` | валідатор відповіді |
| `test_scaffold.py`, `test_publish.py`, `test_common.py` | каркас, регресія публікації, утиліти |

```bash
cd wikipedia-trend-agent && python -m pytest -q     # ~2.5 хв, без мережі
python -m mypy scripts tools --strict
```

### 11.2 Чому це працює на дешевій моделі

| Рішення | Ефект |
|---|---|
| Один вхідний файл | модель не вирішує, що заповнити |
| Одна команда | одна інструкція, один результат |
| Один файл для читання (`metrics.json`) | мінімум контексту |
| Уся арифметика в коді | модель цитує, а не рахує |
| `progressive disclosure` | `SKILL.md` + **не більше одного** reference-файлу на питання |
| Fail-closed словники | модель не отримує «приблизну» відповідь англійською |
| Агрегована валідація | одна ітерація виправлення замість десяти |
| Код виходу = інструкція | `2` → виправ spec; інше → повідом дослівно |

### 11.3 Оцінка: `tools/validate_answer.py`

Твердження репозиторію: **дешева або безкоштовна модель може користуватися навичкою, не вигадуючи числа.** Ось як це перевіряється.

```bash
python tools/validate_answer.py --answer out/answer.md --out out
```

Вхід: `metrics.json` **і нічого іншого**. Не `charts.json`, не `report.md`, не `series.csv`. Відповідь грейдиться проти того самого єдиного джерела істини, з якого написано звіт.

| Властивість | Що перевіряє |
|---|---|
| `numbers_present` | кожне число у відповіді присутнє в `metrics.json` цього запуску |
| `low_is_hypothesis` | серія з `low` сформульована як гіпотеза, а не як висновок |
| `as_of_present` | дата актуальності зазначена, в єдиному канонічному написанні |
| `sources_and_limitations` | обидва розділи присутні **і** мають непорожнє тіло |

Код виходу: `0` — чотири `PASS`; `1` — є `FAIL <property>: <detail>`; `2` — проблема самого інструмента.

Правило перевірки чисел повністю:

```
ACCEPTED FORMS (замкнена множина, без tolerance band):
  str(v)   f"{v:+.1f}   f"{v:,.1f}   f"{v:+}"   f"{v:.1f}   f"{v:,.0f}"
  str(int(v))  str(round(v,0))  str(round(v,1))
  + варіанти з U+00A0 і пробілом замість коми
```

**Чому немає tolerance band.** Допуск — це рівно те, що пропускає неправильне число: якщо 16.7 проходить, бо 16.6 теж проходив би, перевірка декоративна.

Звільнення від перевірки: компоненти дат (`YYYY-MM-DD`, `YYYYMMDD`, `HH:MM`), версії (`0.1.0`) — визначається за **навколишніми** символами, а не внутрішніми.

Свідомо **не** покрито (і це записано в runbook, а не приховано): числа прописами, числа всередині зображення, і **відсоток, виведений моделлю з двох значень `metrics.json`**. Похідне відношення — це нове число, і `numbers_present` справедливо його забракує. Ліки — правило в `INTERPRETATION.md`, а не послаблення перевірки.

Кожен провал пише заглушку в `out/eval/regression/` з дайджестом байтів відповіді, SHA-256 `metrics.json`, зачепним токеном і версією валідатора. Промоція справжнього провалу в `tests/fixtures/eval/` перетворює його на регресійний тест.

**Сфера, сказана прямо:** репозиторій постачає валідатор і runbook, **а не клієнта**. Живий запуск на дешевій моделі — крок власника; жоден код тут його не виконує і не тримає ключів.

---

## 12. Експлуатація та розвиток

### 12.1 Встановлення

```bash
pip install -r requirements.txt          # matplotlib>=3.11, wheels-only
export WTI_USER_AGENT="wikipedia-trend-agent/0.1.0 (you@yourdomain.tld) python-urllib"
```

Навичка — це **директорія**: скопіюйте `wikipedia-trend-agent/` поруч із текою skills вашого агента. Назва директорії та `name:` у `SKILL.md` мають збігатися — це правило активації agentskills.io, і його контролює `tests/test_packaging.py`.

`user_agent()` **fail-fast** і відмовляє, якщо значення порожнє, містить керуючі символи, довше за 256 code points або містить плейсхолдер (`contact` або `example.org`):

```
invalid_ua: User-Agent is empty, non-descriptive, uses a placeholder, is over 256
code points, or contains controls; set WTI_USER_AGENT to a descriptive value
(e.g. 'wikipedia-trend-agent/0.1.0 (you@example.com) python-urllib')
```

Відмова — це `SystemExit` **до** будь-якого мережевого виклику, тому плейсхолдерна ідентифікація не може потрапити в мережу.

| Змінна | Обов'язкова | Дефолт |
|---|---|---|
| `WTI_USER_AGENT` | так, до будь-якого мережевого виклику | плейсхолдер, який **відмовляє** |
| `WTI_CACHE` | ні | `.cache` |
| `WTI_TTL_HOURS` | ні | `24` (`0` = примусовий refetch) |

Жодного API-ключа, токена чи акаунта. Єдина «валіта» — спосіб зв'язатися з людиною щодо трафіку.

### 12.2 Повний робочий цикл

```bash
# 1. зібрати докази
python scripts/resolve_articles.py --topic "..." --projects pl.wikipedia --out out/resolved.json

# 2. переглянути out/resolved.json: search_hits, redirect_chain, volume, status, recommendation
#    → за потреби уточнити запит у користувача

# 3. підтвердити offline
python scripts/resolve_articles.py --topic "..." --projects pl.wikipedia --out out/resolved.json \
  --select pl.wikipedia=<percent-encoded article> --reason "..."

# 4. написати out/spec.json (тільки після status="confirmed")

# 5. виконати pipeline
python scripts/run_all.py --spec out/spec.json --out out

# 6. прочитати out/metrics.json і скласти відповідь
```

### 12.3 Діагностичний шлях

```bash
python scripts/fetch_pageviews.py --spec out/spec.json --out out   # -> series.csv
python scripts/analyze_trends.py  --spec out/spec.json --out out   # -> metrics.json
python scripts/make_charts.py     --spec out/spec.json --out out   # -> PNG + charts.json
python scripts/build_report.py    --spec out/spec.json --out out   # -> report.md + manifest
```

`make_charts.py` приймає `--log-scale`; `run_all.py` свідомо **не** передає його, тому одна команда завжди дає лінійний режим, а log — це окремий запуск стадії.

### 12.4 Чек-лист верифікації (із SKILL.md)

- [ ] `resolved.json` дійшов до `status="confirmed"` з одним selection на кожен запитаний проєкт
- [ ] `run_all.py` вийшов із `0`, або код прочитано за таблицею §4.5 і відпрацьовано
- [ ] `metrics.json` існує, і кожне число у відповіді йому відповідає
- [ ] `as_of` зазначено (останній повний день даних)
- [ ] `confidence` + `confidence_reasons` процитовано дослівно
- [ ] `trend_direction` подано як значення, а не як «інтерес зростає»; `inconclusive` і `noise` — як відповіді
- [ ] кожен відсоток — варіант `clean`, із базою обсягу поруч
- [ ] припущення й обмеження перелічено; `low` сформульовано як гіпотезу
- [ ] `python -m pytest -q` зелений

### 12.5 Як додавати можливості

| Що додати | Що змінити | Порядок |
|---|---|---|
| **Мову** | `REPORT_TOKENS`, `REASON_TOKENS`, `CONFIDENCE_TOKEN_KEYS`, `TREND_TOKEN_KEYS` у `build_report.py` + `CHART_TOKENS` у `make_charts.py` | частковий додаток = **відмова**, не fallback |
| **Тип графіка** | інвентар `2N+1` (CONTRACTS §7.2.0) + розподіл ключів (§7.2.2) | спершу amendment контракту, потім реалізація |
| **Метрику** | CONTRACTS §2 → `test_contracts.py` → `REPORT_TOKENS` + граматика `metrics_shown` (§8.6) | контракт → тест → слова → рендер |
| **Джерело** | `common.py` (`Transport`, кеш) + нова стадія | не порушувати інвентар `2N+1` без amendment |

### 12.6 Статус v0.1

**Робить:** fetch → analyze → chart → report однією командою; підтверджуваний pre-stage резолвера; заморожений JSON-контракт; чотиривластивий валідатор відповіді; репетиція чистого клону однією командою.

**Свідомо не робить:**

- **PDF.** Масив `formats` у `report.manifest.json` — готовий слот; PDF це один доданий об'єкт і один файл.
- **Кросс-серійні агрегати.** Звіт свідомо не публікує жодного числа, що комбінує серії: будь-яке таке число — друга похідна.
- **Прогнози.** Нічого не проєктує.
- **Звіт на `ja`.** Chart приймає `ja`, report — ні.

### 12.7 Standing rules

1. Жодне значення не перераховується чи не трансформується нижче за стадію, яка його опублікувала (ANAL-06).
2. Жодних tolerance band на перевірених числах.
3. Жодної нової рантайм-залежності без явного рішення — одна пачка і є весь сенс бюджету залежностей.
4. `exit 2` = виправ spec. Не переписуй код.

---

## 13. Додатки

### Додаток A. Словник

| Термін | Значення |
|---|---|
| `spec.json` | єдиний вхідний контракт: що саме вимірюємо |
| `series` | одна вимірювана одиниця: проєкт + стаття + підпис |
| `resolved.json` | доказова база: що можна було вибрати і чому |
| `series.csv` | сирі денні значення; відсутній день = відсутній рядок |
| `metrics.json` | **єдине джерело істини** для всіх чисел у відповіді |
| `charts.json` / `report.manifest.json` | маніфести зображень і звіту з ланцюгом SHA-256 |
| `clean` | варіант зростання без аномальних днів |
| `raw` | зростання з усіма днями, включно зі спайками |
| `anomaly` | точка, що відхиляється від ковзної медіани більш ніж на 3.5 MAD |
| `confidence` | рівень від −2 до 5 за прозорою рубрикою, з reasons |
| `trend_direction` | `up` / `down` / `flat` / `noise` / `inconclusive` |
| `noise floor` | 1000 переглядів / 30 днів |
| `not_loaded` | 404, дані ще не опубліковані (≤ 2 дні) |
| `no_views` | 404, даних немає і не буде (> 2 дні) |
| `candidate-bounded` | підтвердити можна лише те, що discovery уже запропонував |
| `as_of` | останній повний день даних, глобальний max-date |
| `ANAL-06` | правило: не перераховувати опубліковане значення |
| `fail-closed` | відмова замість fallback |

### Додаток B. Карта файлів

| Хочеті зрозуміти… | Дивіться |
|---|---|
| Як модель має себе вести | `SKILL.md` |
| Які поля існують у кожному JSON | `references/CONTRACTS.md` |
| Як читати `confidence` / `trend_direction` / `null` | `references/INTERPRETATION.md` |
| UA, throttle, retry, 404, кеш | `references/API_ACCESS.md` |
| Чому ці числа легко прочитати неправильно | `references/DATA_CAVEATS.md` |
| Приклад spec | `assets/example.intermittent-fasting.json` |
| Коди проєктів і їхнє походження | `assets/wikipedia-projects.json` |
| Живий вивід звіту | `README.md` |

### Додаток C. Шпаргалка запитів

```text
# єдиний розділ
Чи зростає інтерес до теми X у N.-му розділі Wikipedia?

# порівняння мов
Порівняй зростання інтересу до теми X у польській і чеській Wikipedia за останні два роки.

# вибір напрямку
Яку мову розвівати наступною: Польща, Чехія чи Угорщина? Потрібне зростання та обсяг, а не лише напрям.

# довіра до тренду
Наскільки можна довіряти зростанню інтересу до теми X?

# діагностика спайку
Чи зростання до теми X реальне, або його дає один аномальний день?
```

### Додаток D. Швидка відповідь на помилки

| Повідомлення | Причина | Дія |
|---|---|---|
| `spec.json validation failed (N errors)` | порушення контракту | виправити всі N рядків зі stderr |
| `HTTP 403 — set a real WTI_USER_AGENT contact and retry` | поганий/placeholder UA | встановити `WTI_USER_AGENT` з реальною адресою |
| `invalid_ua: User-Agent is …uses a placeholder…` | плейсхолдер у змінній | замінити `contact@example.org` на свою адресу |
| `data not yet loaded — retry later` | 404 `not_loaded` | зачекати або звузити вікно |
| `no views for the requested window` | 404 `no_views` | інша стаття або інше вікно |
| `selection is not one exact saved candidate: P=V` | `--select` поза списком кандидатів | узяти `article` із `candidates[]` |
| `ambiguous project requires a reason: CODE` | не вказано `--reason` | додати обґрунтування |
| `no report tokens for language: ja` | report не підтримує мову | змінити `language` на `en`/`uk` |
| `no chart tokens for language: XX` | немає словника для графіка | додати мову в `CHART_TOKENS` |
| `series id is not filename-safe: 'X'` | `id` містить неприпустимі символи | замінити на `[A-Za-z0-9._-]+` |
| `run_all: stage charts failed: exit 1` + warning про шрифт | tofu на CJK | встановити fallback-шрифт або змінити мову |
| `run_all: stage report failed: exited 1` після успішного charts | асиметрія мов (див. §9.5) | прочитати stderr-рядок про мову, **не** вважати запуск успішним |

---

**Джерела:** Wikimedia Pageviews API (<https://wikimedia.org/api/rest_v1/metrics/pageviews/>), *MediaWiki API: Etiquette* і *API:Etiquette/Rate limits* на meta.wikimedia.org, Agent Skills specification (<https://agentskills.io/specification>).
**Ліцензія:** Apache-2.0, повний текст у [`LICENSE`](LICENSE). Усі розділи 6–9 походять із реалізації v0.1.0 і відтворюють її контракти; зміни вимагають amendment `references/CONTRACTS.md` **спершу**, реалізації — **другим**.

## Формат виводу: PDF (необов'язково, `--pdf`)

П'ятий формат постачання, **opt-in**. Ті самі числа й ті самі локалізовані рядки, що й
`report.md`; верстка інша, бо таблиця на 11 колонок не вміщується в A4 — в PDF вона
замінена карткою на серію.

```bash
pip install -r requirements-pdf.txt
winget install Typst.Typst --silent --accept-package-agreements
python scripts/run_all.py --spec out/spec.json --out out --pdf
```

`requirements.txt` лишається ** одним рядком**: бюджет рантайм-залежностей не змінено.
Паритет перевіряється механічно: `set(pdf.shown) == set(manifest["metrics_shown"])`.

Єдине свідоме відхилення від побайтового паритету: PDF показує назву статті
**percent-декодованою**, бо `%D0%91%D1%96...` не читається. У `metrics.json` і в
`report.md` slug лишається encoded — він є ключем кешу AQS.
