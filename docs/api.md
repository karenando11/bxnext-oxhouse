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
