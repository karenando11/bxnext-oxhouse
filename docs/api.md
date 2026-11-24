# OxHouse Research API

JSON over HTTP under `/api/`. The React frontend is the only intended client. Sign-in is a session
cookie with CSRF protection.

## Authentication

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/auth/csrf` | GET | Sets the `csrftoken` cookie. Call once at start-up. |
| `/api/auth/login` | POST | Body `{"username", "password"}`. Sets the session cookie. |
| `/api/auth/logout` | POST | Ends the session. |
| `/api/auth/me` | GET | `{"authenticated": bool, "username"}`. Works when signed out. |

Send the token from the `csrftoken` cookie in an `X-CSRFToken` header on every POST and DELETE.
Every other endpoint answers an anonymous request with **401** and
`{"error": "authentication_required", "login_url": "/api/auth/login"}`. An API has nothing to
redirect to, so the frontend sends the user to its sign-in screen on a 401.

Errors are `{"error": <code>, "message": <text>}` with a 4xx status.

## Screener

All screener reads come from the `company_page` read model (the latest completed score run). Filters
are query parameters; any filter left out takes its value from the default screen (or from
`screen=<id>`, a saved screen, when given).

| Parameter | Meaning |
|---|---|
| `mcap_min_musd`, `mcap_max_musd` | Market cap bounds, USD millions |
| `price_min` | Minimum price, USD |
| `avg_vol_min_k` | Minimum average volume, thousands of shares |
| `shares_outstanding_max_m` | Maximum shares outstanding, millions |
| `composite_floor`, `outlier_floor` | Minimum scores |
| `include_sparse` | `true` or `false` |
| `screen` | Id of a saved screen to start from |

Pass `off`, `null`, `none` or an empty value to switch a numeric filter off. A bad value gives
**400** `invalid_filters` with the parameter named in the message.

| Endpoint | Returns |
|---|---|
| `GET /api/screener/` | The passing companies, ranked by composite (ties by ticker): `rows`, `count`, `universe_n`, the `filters` used, and freshness (`as_of_date`, `age_days`, `stale`, `stale_banner`, `is_fixture`). Each row has ticker, company, industry, market cap, price, composite, Cash Health, Milestone, Outlier, a `display` object with scores rounded half up to one decimal, peer group, `next_catalyst` and runway. |
| `GET /api/screener/export.csv` | The same rows as CSV. Text cells starting with `=`, `+`, `-` or `@` are prefixed with a quote. |
| `GET /api/screener/why/<ticker>` | `passed`, `rank` and `failures`: every failed filter with `reason`, `filter`, `actual`, `threshold` and `comparison`. 404 for an unknown ticker. |
| `GET /api/screener/near-the-line` | Companies that fail exactly one filter (a composite-only failure counts within `margin`, default 0.5), each with its failures. |
| `GET /api/screener/funnel?days=30` | Who entered and left the default list, by day, with the reason. |

`stale` is true when the latest score run is more than the `stale.snapshot_days` setting (default 3)
days old.

## Saved screens

Shared with all users; the owner is recorded. A name is unique per owner.

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/screens/` | GET | All screens, the default first. |
| `/api/screens/` | POST | Body `{"name", "filters"}`. Filters are validated; unknown keys give 400. Returns 201. |
| `/api/screens/<id>` | GET | One screen. |
| `/api/screens/<id>` | DELETE | Owner only. The default screen cannot be deleted. |

## Company page

`GET /api/company/<ticker>` returns everything the company page shows, in one response. The ticker
is case-insensitive. **404** `unknown_ticker` when no company page exists for it; **401** when not
signed in. The response is never cached. It is built from one read of the `company_page` read model
(the latest completed score run) plus the trial lists derived from the stored studies; nothing
else is queried.

Every number comes as a raw `value`; where the page shows it formatted there is also a `display`
string (`"$5.39"`, `"$308M"`, `"-29.61%"`). Scores carry `display` rounded half up to one decimal.
A value that is missing is `null`, and its reason is in `fields` (see below).

| Key | Content |
|---|---|
| `ticker`, `company`, `as_of_date`, `today`, `is_fixture` | Identity and the date the page describes. `is_fixture` is true when the data came from a fixture load, not a live collection. |
| `header` | Ticker, company, industry, `peer_group` (`mcap_bucket`, `key`, `peer_count`, `is_sparse`), `as_of_date`, `market_data_as_of`. |
| `chart` | `{"provider": "tradingview", "symbol": "<ticker>"}`. The frontend embeds the widget; nothing is stored. |
| `banners` | The stale-data banners that apply: a list of `{code, message, block}`. Codes: `earnings_passed` ("Earnings date has passed"), `catalyst_passed`, `cash_stale` (balance sheet over 120 days old), `carried_input` ("Using data from N days ago"), `market_stale` (snapshot over 3 trading days old), `short_volume_stale`, `insider_stale`, `fixture`. Market-data age counts trading days, so a long weekend or a holiday does not raise a false banner. |
| `tiles` | `price`, `market_cap`, `vs_52w_high`, `vs_200d_ma`, `short_float`, `inst_ownership`, `cash_per_share`, `earnings_date`, and the placeholders `fair_value`, `bear_case`, `bull_case`, `upside` (`{"status": "not_yet_available"}`). |
| `momentum` | `rsi_14`, `avg_volume_k`, `rel_volume`, `perf_week`, `perf_month`, `perf_quarter`, `perf_ytd`. |
| `balance_sheet` | `cash_per_share`, `quick_ratio`, `debt_to_equity`, `gross_margin`, `op_margin`. |
| `ownership` | `short_float`, `inst_ownership`, `inst_transactions`, `insider_ownership`. |
| `scores` | `composite` (Cash + Cat. Score), `spring`, `setup`, `timing`, `outlier`, each `{label, value, display}`; `recency` (`shown: false`); `components` (every intermediate term, to expand a score); `carried_inputs`, `missing_inputs`, `input_resolution` (runway and catalyst: `state` fresh, carried, expired or missing, `age_days`, `reason`); `scored_at`. |
| `runway` | `months`, `months_label` (`"360+"` when capped), `quarters`, `rating` (Critical, Short, Moderate, Long), `label` (`"cash-flow positive"` when the company is, in which case quarters and rating are null), `capped`, `cap_reason`, `cash_usd`, `cash_source_tags`, `operating_burn_monthly_usd`, `trial_burn_monthly_usd`, `burn_total_monthly_usd`, `trial_burn_lines` (per phase: count, rate, amount), `burn_source`, `balance_sheet_date`, `calc_date`, `financing_date`, `status`, `reasons`, `resolution`. |
| `catalyst` | `clinical_date`, `phase`, `overdue`, `source_nct`, `next_event_source` (clinical, financing or none), `financing_date`, `fallback_completion`, `candidates` (every study considered, with `eligible`, `reject_reason`, `chosen`), `status`, `as_of_date`, `reasons`, `resolution`. |
| `trials` | `total_studies`, `active_phase_3`, `active_phase_2`, `latest_trial`, `latest_status`, `next_milestone` (card: study, phase, date, `quarters_out`, `drug`), `pipeline` (`total`, `phase_chips`, `studies`), `indications` (not de-duplicated), `upcoming_completions`, `reasons` (`no_trials` for a company with no studies). |
| `short_selling` | `label` (`"short volume, not short interest"`), `latest_ratio`, `trend`, `delta_vs_30d_pts`, `latest_trade_date`, `age_days`, `is_stale`, `reasons`. |
| `insider` | `filings_90d`, `open_market_buys`, `open_market_sales`, `officers_directors_buying`, `officers_directors_selling`, `pct_sale_dollars_under_10b5_1`, `age_days`, `is_stale`, `reasons`. |
| `vintage` | The footer: for each block its `as_of` date, its `source` and where relevant `age_days`, `balance_sheet_date` or `produced_at`. |
| `fields` | All 55 fields of the company-page field list, keyed by name, each `{n, label, value, reason}`. `value` is set or `reason` is: a reason code such as `non_usd_only`, `cash_missing`, `no_trials`, `not_in_finra_file`, `company_cash_positive`, `fixture_not_available`, or one of three the API adds for values no collector stores: `not_reported` (the source left it blank), `no_open_market_sales` (no sales in the window, so no 10b5-1 share) and `not_available`. A consumer can render any field from here without knowing which block holds it. |

## Pipeline health

`GET /api/health` (sign-in required) reports what the last daily batch did and what needs
attention. It is built from the pipeline tables, not from the company-page read model.

| Key | Content |
|---|---|
| `last_batch` | The latest batch: run id, run date, market date, status, start, finish, duration, `failed_steps` and every step (`step_name`, `status`, `rows_in`, `rows_out`, `error_text`, `detail`) |
| `sources` | One entry per feed: `last_success`, `last_failure`, `last_failure_reason` |
| `quality_checks` | The data-quality checks of the last batch, failures first (check, scope, status, expected, observed, detail) |
| `inputs` | For the latest complete score run: how many companies have a `fresh`, `carried`, `expired` or `missing` runway and catalyst, how many carried values are older than half their maximum age, and counts of null inputs |
| `active_alerts` | Alert conditions raised now: key, kind, subject, when raised, emails sent |
| `history` | The last 30 batch runs, newest first, without their steps |

### Alerts

Alerts are emailed to `ALERT_EMAIL_TO` (comma-separated; the mail server is set with `EMAIL_HOST`,
`EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS`, and the sender with
`ALERT_EMAIL_FROM`). With no recipients they go to the error log. A condition sends one email when
it is raised and nothing more while it persists; after it clears, a recurrence sends again.

| Condition | Key |
|---|---|
| The daily batch fails (Finviz, scoring or publish, or any other failure) | `finviz_failed`, `score_failed`, `publish_failed`, `batch_failed` |
| The batch is still running at 07:00 | `batch_overrun` |
| A source fails on two consecutive batch days | `source_failing:<feed>` |
| A Finviz column is blank across the whole universe | `blank_column:finviz.<column>` |
| The Finviz row count changes by more than 20 percent | `finviz_row_count` |
| Form 4 parse failures exceed the limit (setting `alerts.form4_parse_failures_max`, default 5) | `form4_parse_failures` |
| FINRA has no file for a trading day in the last ten days | `finra_missing:<date>` |
