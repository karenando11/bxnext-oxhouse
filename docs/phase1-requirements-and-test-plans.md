# OxHouse Research: Phase 1 Requirements and Test Plans

Status: proposal for review. Nothing here has been implemented, and nothing has been written to the repository.

This document turns the 24 Phase 1 steps (0 to 20, plus 11A, 11B and 13A) into Gherkin requirements with acceptance criteria, open items and test plans. It builds on the Phase 1 architecture and data model v5. Since v4, every company-page field is collected for every company in the universe each day, which is why FINRA short volume (11A) and Form 4 insider activity (11B) are Phase 1 steps. v5 consolidates storage to 23 tables (two metric tables become views, four explanatory tables become JSONB on their parent rows, reason codes become a list in code) and adds a rule that the API reads only one read model, the `company_page` view.

---

## How to read this document

- **Requirement:** a Gherkin `Feature` with scenarios. Each scenario is written to be directly automatable.
- **Acceptance criteria:** numbered checks (`A<step>.<n>`) that must all hold for the step to be done.
- **Open items:** questions or data we still need, each with the **default assumption** the build will use if unanswered.
- **Test plan:** a table of tests (`T<step>.<n>`) with level, data, and pass condition.

Test levels: **Unit** (pure code, no database, no network), **Integration** (database, no network), **Contract** (recorded responses from external sources), **E2E** (whole stage or browser), **Manual**.

---

## Fixture catalogue

| ID | Fixture | Contents | Status |
|---|---|---|---|
| F1 | `golden_export_2026-10-12.csv` | 1,053 companies, 87 columns: Finviz fields, runway, trial summary, catalyst fields, and every stored score for the pass at 2026-10-12 16:30 ET | Available |
| F2 | `edgar_runway_export_2026-10-14.csv` | 1,103 rows: cash, monthly burn, burn source, reported and recomputed runway, notes | Available |
| F3 | Nine fixture tickers | KTTA, RZLT, XBIT, YDES, APLM, SMMT, PACS, INDV, ALPS with composite, Cash Health and Milestone (see below) | Available |
| F4 | Reference workbook calculator example | Eight-peer cluster plus EXAMPLE-TICKER: Outlier 10 (clamped), Divergence 22.418, Recency 1.0, Composite 7.835 with Cash 7.2 and Milestone 8.5 | Available |
| F5 | Cached ClinicalTrials.gov studies | Studies for 86 tickers pulled live on 2026-10-13. Not identical to the 10-12 state | Available (held by the analyst) |
| F6 | Raw Finviz Elite CSV (view 151, `f=sec_healthcare`) | One unprocessed download | **To obtain** |
| F7 | Recorded SEC responses | `company_tickers.json`, `submissions`, `companyfacts` for KTTA, PACS, an IFRS filer (for example BNTX), a non-USD filer, a ticker with no CIK, a 10-K/A filer | **To record** (SEC blocked the analyst's shared IP) |
| F8 | `company_info_fields.xlsx` | Field list, sources, derivations, display formats | Available |
| F9 | ABEO research report (PDF) | Page layout and tile values | Available |
| F10 | Finviz column map | Internal key to Finviz header mapping (reference workbook, sheet "Finviz Column Map") | Available |
| F11 | Recorded FINRA short-volume files | Three consecutive trading days, a weekend date (no file), and a day with a universe ticker missing | **To record** |
| F12 | Recorded Form 4 XML documents | An officer purchase, a sale under a 10b5-1 plan, a sale not under a plan, a multi-owner filing, a gift (code G), an amendment (4/A), and one that fails to parse | **To record** |

F3 expected values:

| Ticker | Composite | Cash Health | Milestone | Outlier | Recency | Divergence |
|---|---|---|---|---|---|---|
| KTTA | 8.4927 | 10.0 | 8.0 | 10 | 1.0 | 14.3 |
| RZLT | 7.6032 | 8.2 | 8.1 | 7.5 | 1.0 | 17.3 |
| XBIT | 7.5059 | 10.0 | 7.2 | 5.2 | 1.0 | 8.0 |
| YDES | 4.2500 | 2.5 | 3.0 | 10 | 0.75 | 10.4 |
| APLM | 4.2480 | 1.3 | 8.6 | 2.4 | 0.5 | 0.3 |
| SMMT | 4.2460 | 4.9 | 5.2 | 4.2 | 0.3 | 0.8 |
| PACS | 1.9104 | 0.8 | 3.0 | 2.9 | 0.3 | -2.0 |
| INDV | 1.8683 | 1.0 | 3.0 | 1.3 | 0.3 | 0.6 |
| ALPS | 1.6595 | 0.4 | 3.0 | 2.3 | 0.3 | -3.1 |

## Global requirements (apply to every step)

```gherkin
Feature: Cross-cutting engineering rules

  Scenario: Determinism
    Given identical stored inputs and an identical configuration version
    When a computation is run twice
    Then the outputs are identical to the last stored digit

  Scenario: No network in unit tests
    Given the unit test suite is running
    When any code attempts an outbound network connection
    Then the test fails

  Scenario: Idempotent loads
    Given a load has completed for a given source, scope and date
    When the same load runs again with the same content
    Then no duplicate rows are created

  Scenario: Every null has a reason
    Given any derived value is null
    Then a reason code from the fixed reason-code list is stored beside it

  Scenario: Units are explicit
    Given any stored numeric column
    Then its unit is documented in the model (market cap in USD millions, shares in millions,
         average volume in thousands of shares, percentages as plain numbers, money in USD)

  Scenario: Time conventions
    Given any stored timestamp
    Then it is timestamptz in UTC
    And market dates use the US Eastern trading date

  Scenario: Universe-wide daily coverage
    Given the 55 fields of the company page (Appendix B)
    When the daily pipeline completes
    Then every field is collected or derived for every company in the universe, not only the screened names
    And deep collection of other sources applies only to the coverage list of about 80 companies
    And any field that cannot be filled carries a reason code

  Scenario: API reads only the read model
    Given the API serves the screener table, the company page and the CSV export
    Then each reads only from the company_page read model
    And no API endpoint joins base tables per request
    And each page field is defined in exactly one place
```

Global acceptance criteria: **G1** ruff and unit tests pass in CI on every change. **G2** coverage of the scoring package is at least 90%. **G3** no secret appears in the repository or logs. **G4** every external call is recorded in `source_call`. **G5** all 55 page fields are collected or derived for every universe company each day (Appendix B). **G6** the API reads only the `company_page` read model, never the base tables. The Django project lives in `backend/`; test paths below are relative to it.

---

## Step 0: Project skeleton and CI

**Status: done** (commit 0b9d14e, restructured into `backend/` in 38d0314).

**Depends on:** none. **Size:** S.

```gherkin
Feature: Project skeleton and CI

  Scenario: Clean checkout builds
    Given a clean checkout of the repository
    When a developer runs the documented bootstrap command
    Then the Django project starts against a local PostgreSQL container
    And all migrations apply to an empty database

  Scenario: Continuous integration
    Given a pull request is opened
    When the CI workflow runs
    Then lint, type checks and unit tests run
    And the pull request cannot merge if any fail

  Scenario: Health endpoint
    Given the application is running
    When a client requests /healthz
    Then the response is 200
    And the body reports database connectivity and the current schema version

  Scenario: Configuration from environment
    Given no secrets are committed
    When the application starts without required environment variables
    Then it fails fast with a message naming the missing variable
```

**Acceptance criteria**
- A0.1 Bootstrap on a clean machine takes one documented command and under 10 minutes.
- A0.2 CI runs on every pull request and reports status.
- A0.3 `/healthz` returns 200 only when the database is reachable.
- A0.4 Settings are read from environment variables with a documented list.
- A0.5 The apps listed in the architecture exist as empty modules.

**Open items**
| Item | Default |
|---|---|
| Python minor version and dependency tool | Python 3.12, `uv` |
| Repository host and branch protection rules | GitHub, protected default branch |

**Test plan**
| ID | Level | Test | Pass condition |
|---|---|---|---|
| T0.1 | E2E | Fresh clone, bootstrap, `pytest` | Exit code 0 |
| T0.2 | Integration | Apply all migrations to empty DB, then `makemigrations --check` | No pending changes |
| T0.3 | Integration | GET `/healthz` with DB up, then with DB stopped | 200, then 503 |
| T0.4 | Unit | Start settings with each required variable removed | Named failure each time |
| T0.5 | Manual | Open a PR with a failing lint | Merge blocked |

---

## Step 1: Scoring, Outlier, Divergence and Recency

**Status: done** (commit 5cc1522; 70 new tests, 107 in total; code in `backend/scoring_engine/`, tests in `backend/tests/scoring_engine/`). **Depends on:** none. **Size:** M. Pure Python, no database.

```gherkin
Feature: Peer-relative scores (Outlier, Divergence, Recency)

  Background:
    Given the universe is the Finviz Elite export filtered to the Healthcare sector only (f=sec_healthcare)
    And peer groups are formed only from companies in that universe
    And the dimensions perf_ytd, perf_m, perf_w, rsi, inst_own, short_float and vol_m
    And the peer group key is "<industry>|<market cap bucket>"
    And buckets are Nano below 25, Micro below 100, Small below 500, otherwise Mid (market cap in USD millions)

  Scenario Outline: Market cap bucket
    Given a company with market cap <mcap>
    Then its bucket is <bucket>
    Examples:
      | mcap | bucket |
      | 14.95 | Nano |
      | 99.9 | Micro |
      | 307.94 | Small |
      | 1500 | Mid |
      | (missing) | ? |

  Scenario: Robust z-score
    Given a peer group of at least 10 companies with at least 5 values in a dimension
    When z is computed for a company
    Then z equals (value minus the group median) divided by the unscaled median absolute deviation
    And z is 0 when the deviation is 0

  Scenario: Sparse peer group
    Given a peer group with fewer than 10 companies
    Then Outlier is 7
    And Divergence is 0
    And Recency is 1.0

  Scenario: No usable dimensions
    Given a company in a non-sparse group with no usable dimension values
    Then Outlier is 5

  Scenario: Outlier
    Given z-scores for the available dimensions
    Then Outlier equals the square root of the mean of z squared, times 2.5, clamped to 0..10

  Scenario: Divergence
    Then Divergence equals the sum of z for perf_ytd, perf_m, perf_w, rsi and inst_own
    And plus 0.3 times the absolute z for short_float and vol_m, each only when its absolute z exceeds 1

  Scenario Outline: Recency
    Given |z_week| = <w>, |z_month| = <m>, |z_ytd| = <y>
    Then Recency is <recency>
    Examples:
      | w | m | y | recency |
      | 2.0 | 1.0 | 0.5 | 1.0 |
      | 0.5 | 2.0 | 1.0 | 0.75 |
      | 0.5 | 0.4 | 1.5 | 0.5 |
      | 0.5 | 0.4 | 0.3 | 0.3 |

  Scenario: Reference calculator example
    Given the eight-peer cluster and EXAMPLE-TICKER from the reference workbook
    Then Outlier is 10 (clamped), Divergence is 22.418 and Recency is 1.0

  Scenario: Peers come only from the healthcare universe
    Given the golden universe of 1,053 healthcare companies in 11 industries
    Then a company's peers are only the companies sharing its industry and market cap bucket
    And no non-healthcare company is ever a peer

  Scenario: Golden universe
    Given the 1,053 companies in the golden export
    When scores are computed over the whole universe
    Then every Outlier, Divergence and Recency matches the stored value
```

**Acceptance criteria**
- A1.1 1,053 of 1,053 rows match for all three scores (Outlier and Divergence to one decimal, Recency exactly).
- A1.2 The calculator example reproduces to three decimals.
- A1.3 The module imports no database or network code.
- A1.4 Full-universe computation takes under 2 seconds.
- A1.5 Round-half-up on the exact binary value: 1.25 displays as 1.3, 4.55 as 4.5; Recency 0.75 is not rounded.

**Open items**
| Item | Default |
|---|---|
| Recency value when a non-sparse company has no usable dimension | 0.3 (not exercised by the golden data) |
| Behaviour when industry is missing | Group key uses "Unknown" |
| Rules the golden data does not pin: `min_values` of 5, whether a group of exactly 10 is sparse, the no-data Outlier of 5 and Recency of 0.3 | Documentation values kept; recorded in the `TopologyConfig` docstring |
| The healthcare-only universe includes all 11 Finviz healthcare industries, not only biotech (594 of 1,053 are biotech; 114 of the 139 default-screen names are biotech) | No industry filter in the default screen. An industry filter is available as an optional screener control (to confirm) |

**Test plan**
| ID | Level | Test | Data | Pass condition |
|---|---|---|---|---|
| T1.1 | Unit | Bucket boundaries and missing market cap | table above | Exact |
| T1.2 | Unit | z-score with MAD 0, with fewer than 5 values | synthetic | z 0, dimension skipped |
| T1.3 | Unit | Sparse group defaults | synthetic group of 9 | 7, 0, 1.0 |
| T1.4 | Unit | Recency decision table | table above | Exact |
| T1.5 | Unit | Calculator example | F4 | Three-decimal match |
| T1.6 | Unit | Full golden regression | F1 | 1,053 of 1,053 |
| T1.7 | Unit | Rounding utility cases | 1.25, 4.55, 0.75, -0.05 | As specified |
| T1.8 | Unit | Import guard | module source | No DB or network imports |

---

## Step 2: Scoring, Cash Health

**Depends on:** none. **Size:** M. Pure Python.

```gherkin
Feature: Cash Health score

  Background:
    Given the evaluation time "now"

  Scenario: Weighted blend of balance-sheet terms
    Given quick_ratio, cash_sh, price and td_eq are present
    Then each term is bucketed and averaged using weights 1.5, 1.0 and 0.8
    And quick ratio buckets are >=5:10, >=3:8, >=1.5:6, >=1:4, >=0.5:2, else 0
    And cash per share divided by price buckets are >=0.8:10, >=0.5:8, >=0.3:6, >=0.1:4, else 2
    And total debt to equity buckets are <=0.1:10, <=0.3:8, <=0.5:6, <=1:4, <=2:2, else 0

  Scenario: P/Cash fallback
    Given the total weight of available terms is below 1.5
    And pcash is present
    Then a P/Cash term with weight 1.0 is added
    And P/Cash buckets are <=2:9, <=5:7, <=10:5, <=20:3, else 1

  Scenario: No usable inputs
    Given no balance-sheet term is available
    Then the base score is 5

  Scenario Outline: Runway ceiling
    Given runway_months is <runway>
    Then the base score is capped at <cap>
    Examples:
      | runway | cap |
      | 3.06 | 3 |
      | 10.9 | 5 |
      | 15 | 7 |
      | 20 | none |

  Scenario Outline: Catalyst dampener with a clinical date
    Given a clinical catalyst date <months> months from now (30-day months, overdue counts as 0)
    Then the score is multiplied by <factor>
    Examples:
      | months | factor |
      | 0 | 1.0 |
      | 12 | 1.0 |
      | 12.1 | 0.8 |
      | 18.1 | 0.65 |
      | 24.1 | 0.5 |

  Scenario: Trial completion fallback date
    Given no clinical catalyst date
    And a next or latest trial completion date (month-only dates mean the 1st)
    And that date is not more than 90 days in the past
    Then the same month bands apply to that date

  Scenario: Lapsed fallback date with an active late-stage trial
    Given the fallback date is more than 90 days in the past or absent
    And at least one Phase 2 or Phase 3 trial is active
    Then the dampener factor is 1.0

  Scenario: No catalyst of any kind
    Given no usable date and no active Phase 2 or Phase 3 trial
    Then the dampener factor is 0.5

  Scenario Outline: Fixture tickers
    Given the stored inputs for <ticker>
    Then Cash Health displays as <score>
    Examples:
      | ticker | score | why |
      | KTTA | 10.0 | Phase 1 trial completes 2027-09, so the fallback date applies and factor is 1.0 |
      | SMMT | 4.9 | clinical date 2028-04-15 is 18.1 months out, factor 0.65 |
      | APLM | 1.3 | base 2.5, runway cap 3 does not bind, no catalyst date, factor 0.5, rounds half up |
      | YDES | 2.5 | runway 10.9 caps at 5, then factor 0.5 |
      | PACS | 0.8 | base about 1.5, no catalyst, factor 0.5 |

  Scenario: Golden universe
    Given the 1,053 companies in the golden export
    Then every Cash Health value matches the stored value
```

**Acceptance criteria**
- A2.1 1,053 of 1,053 golden rows match; the nine fixtures match.
- A2.2 Null inputs are skipped, never treated as zero.
- A2.3 The function takes plain inputs and a `now` value, with no hidden clock access.
- A2.4 The components (each term's score and weight, the cap, the factor and why) are returned alongside the score.

**Open items**
| Item | Default |
|---|---|
| Behaviour when price is missing or zero | Skip the cash-per-share term |
| Negative debt-to-equity or negative cash per share | Bucket by value (negative debt-to-equity lands in the best bucket); flag for review |
| Runway ceiling uses runway as filed (known quirk) | Kept for parity, switchable in Step 4 |

**Test plan**
| ID | Level | Test | Data | Pass condition |
|---|---|---|---|---|
| T2.1 | Unit | Each bucket boundary for the three terms | synthetic | Exact |
| T2.2 | Unit | P/Cash fallback trigger at weight 1.5 | synthetic | Added only below 1.5 |
| T2.3 | Unit | Runway ceiling table | table above | Exact |
| T2.4 | Unit | Dampener bands including boundaries 12, 18, 24 | table above | Exact |
| T2.5 | Unit | Lapsed fallback date and active-trial exemption | synthetic | Factor 1.0 vs 0.5 |
| T2.6 | Unit | Nine fixtures | F3 | Exact display values |
| T2.7 | Unit | Full golden regression | F1 | 1,053 of 1,053 |
| T2.8 | Unit | Missing price, negative equity | synthetic | As defaults, no exception |

---

## Step 3: Scoring, Milestone and Composite

**Depends on:** Steps 1 and 2. **Size:** M. Pure Python.

```gherkin
Feature: Milestone and Composite

  Scenario: Floor when nothing qualifies
    Given no clinical date, no usable trial completion date and no active Phase 2 or Phase 3 trial
    Then Milestone is 3

  Scenario: Dated clinical readout
    Given a clinical catalyst date and phase
    Then a Phase 3 date scores time_decay(peak 10) at weight 3.0
    And any other phase scores time_decay(peak 9) at weight 2.0
    And time_decay is: <=0 days max(2, peak-5); <=30 max(3, peak-2); <=90 max(5, peak-1);
        <=180 max(7, peak-0.5); <=365 peak; <=540 max(4, peak-5); <=730 2; else 1

  Scenario: Overdue clinical date
    Given the clinical overdue flag is true
    Then the readout is treated as 1 day out regardless of how old the date is

  Scenario: Lapsed readout window
    Given a readout date in the past by less than 90 days and not flagged overdue
    Then it is treated as 1 day out
    But given it is more than 90 days in the past and not flagged overdue
    Then it still scores at the lowest decay value but does not count as an identified catalyst

  Scenario: Active trial counts
    Given active Phase 3 trials n3 and active Phase 2 trials n2
    Then Phase 3 contributes min(4*n3, 10) at weight 1.5 when n3 > 0
    And Phase 2 contributes min(3*n2, 9) at weight 1.0 when n2 > 0

  Scenario: Trial completion fallback
    Given no clinical date was scored
    And the next (else latest) trial completion date is in the future, or lapsed by less than 90 days
    Then it contributes time_decay(peak 9) at weight 1.0

  Scenario: Recruiting bonus
    Given the latest trial status is RECRUITING and at least one Phase 3 trial is active
    Then 7 at weight 0.5 is added

  Scenario Outline: Earnings proximity
    Given an earnings date <days> days from now (past dates count as 0 days)
    And a clinical date <was or was not> scored
    Then the score is <score> at weight <weight>
    Examples:
      | days | clinical date | score | weight |
      | 0 | scored | 10 | 1.0 |
      | -75 | scored | 10 | 1.0 |
      | 20 | not scored | 8 | 2.0 |
      | 100 | not scored | 2 | 2.0 |

  Scenario: Growth and volume terms
    Then eps_next buckets are >50:8, >20:6, >0:4, else 2 at weight 0.8
    And rel_vol buckets are >5:10, >3:8, >2:6, >1.5:4, else 2 at weight 0.5
    And eps_this buckets are >50:7, >20:5, >0:3, else 1 at weight 0.5
    And a null input is skipped

  Scenario Outline: Financing-needed boost
    Given runway_months and months to the next milestone date
    When runway is shorter than months to milestone
    Then the gap selects the boost
    Examples:
      | gap months | boost |
      | 12 or more | 3 |
      | 6 to under 12 | 2 |
      | 3 to under 6 | 1.5 |
      | under 3 | 1 |

  Scenario: Final blend
    Then Milestone equals the weighted average of all included terms plus the boost, clamped to 0..10

  Scenario: Composite
    Then Composite equals clamp(0.30*Cash + 0.35*Milestone + 0.15*Outlier
                               + 0.10*(Recency*10) + 0.10*clamp(Divergence*0.5, -2, 2), 0, 10)
    And it is computed from unrounded component values

  Scenario: APLM stress case
    Given APLM has no clinical date, an earnings date in the past, relative volume 0.08 and runway 3.06 months
    Then Milestone displays as 8.6 because the earnings term scores 10 at weight 2.0 and the boost is 3

  Scenario: Golden universe
    Given the 1,053 golden rows
    Then Milestone matches for the 621 rows that need no hidden eps_this value
    And the remaining 432 rows match when the correct eps_this bucket is supplied
    And Composite matches within rounding for all 1,053
```

**Acceptance criteria**
- A3.1 All 515 floor rows match; all 93 rows without an earnings date match.
- A3.2 621 rows match with `eps_this` absent; the 432 others match with the inferred bucket.
- A3.3 Composite matches within 0.045 on all 1,053 rows (rounding tolerance).
- A3.4 The nine fixtures match.
- A3.5 Components are returned with the scores.

**Open items**
| Item | Default |
|---|---|
| Real `eps_this` values: expected KTTA >50; RZLT, SMMT, IART 0 to 20; MDXG, CNMD, HUM, NBTX 0 or below | The 432-row test is marked expected-fail until the values arrive; ingestion always reads the column |
| `now` definition | The Stage A timestamp (16:30 ET) |
| Stale earnings dates score 10 | Parity quirk, switchable in Step 4 |

**Test plan**
| ID | Level | Test | Data | Pass condition |
|---|---|---|---|---|
| T3.1 | Unit | Floor cases | synthetic and F1 | 515 of 515 |
| T3.2 | Unit | time_decay boundaries | days 0, 30, 90, 180, 365, 540, 730 | Exact |
| T3.3 | Unit | Overdue and lapsed windows | synthetic | As scenarios |
| T3.4 | Unit | Earnings table and clamp | table above | Exact |
| T3.5 | Unit | Boost table, including runway not shorter | synthetic | Exact |
| T3.6 | Unit | Rows without earnings date | F1 (93 rows) | 93 of 93 |
| T3.7 | Unit | Rows needing no `eps_this` | F1 | 621 of 621 |
| T3.8 | Unit | 432 rows with supplied buckets | F1 plus inferred buckets | 432 of 432, marked expected-fail until real values |
| T3.9 | Unit | Composite formula | F1 | Within 0.045 on 1,053 |
| T3.10 | Unit | Nine fixtures | F3 | Exact |

---

## Step 4: Formula configuration and quirk switches

**Depends on:** Steps 1 to 3. **Size:** S.

```gherkin
Feature: Versioned scoring configuration

  Scenario: Parity version
    Given the parity formula version
    When the golden suite runs
    Then every golden test passes unchanged

  Scenario: Configuration drives scoring
    Given a copy of the configuration with the Composite weight for Cash changed from 0.30 to 0.25
    When scores are recomputed
    Then Composite changes accordingly and component scores do not

  Scenario: Published versions are immutable
    Given a formula version has been used by a score run
    When someone edits it
    Then the edit is rejected and a new version is required

  Scenario: Invalid configuration
    Given a configuration with an unknown key or weights that are not numbers
    Then validation fails with a message naming the key

  Scenario Outline: Quirk switches
    Given the switch <switch> is turned off
    Then the behaviour becomes <behaviour>
    Examples:
      | switch | behaviour |
      | earnings_past_is_imminent | past earnings dates are skipped instead of scoring 10 |
      | runway_as_filed | runway used in the ceiling and boost is runway_months minus months since the balance-sheet date, floored at 0 |
      | ocf_ytd_naive_sum | year-to-date cash-flow facts are de-cumulated before burn is computed |
      | trial_burn_on_profitable | trial burn is ignored when operating cash flow is positive |
      | carry_forward | failed fetches null the input instead of carrying the last good value |
```

**Acceptance criteria**
- A4.1 Parity version reproduces the whole golden suite.
- A4.2 Every constant in Steps 1 to 3 is read from configuration.
- A4.3 Each switch has a test showing both behaviours.
- A4.4 A formula version used by a run cannot be modified.

**Open items**
| Item | Default |
|---|---|
| Which switches are on in the first production version | Parity on for scoring quirks, carry-forward on |
| Who approves moving from parity to a fixed version | The product owner |

**Test plan**
| ID | Level | Test | Pass condition |
|---|---|---|---|
| T4.1 | Unit | Golden suite under parity configuration | All pass |
| T4.2 | Unit | Alter each constant, confirm the right output moves | Only the expected output changes |
| T4.3 | Unit | Each switch both ways | As table |
| T4.4 | Unit | Schema validation with bad keys and types | Named errors |
| T4.5 | Integration | Edit a used version | Rejected |

---

## Step 5: Data model and migrations

**Depends on:** Step 0. **Size:** M.

```gherkin
Feature: Phase 1 data model

  Scenario: Tables exist
    Given migrations are applied
    Then the 23 Phase 1 tables in the data model document exist with the specified columns
    And the views short_volume_metric, insider_metric_daily and company_page exist
    And no table exists for reason codes, pipeline steps, trial burn lines, catalyst candidates or peer group statistics

  Scenario: Natural keys are unique
    Then finviz_snapshot is unique on (company_id, as_of_date)
    And runway_calc is unique on (company_id, calc_date)
    And catalyst_signal is unique on (company_id, as_of_date)
    And sec_filing is unique on accession
    And ct_study_version is unique on (nct_id, content_hash)
    And score_result is unique on (score_run_id, company_id)
    And short_volume_daily is unique on (ticker_raw, trade_date, market)
    And sec_form4_filing is unique on accession

  Scenario: Only one current ticker
    Then ticker_history allows at most one open-ended row per ticker

  Scenario: Seed data
    Then default settings, the default screen definition and the parity formula version
         are present after migration
    And the reason codes are a fixed list in code, and a null stored with an unknown reason code is rejected

  Scenario: Explanatory detail lives on the parent row
    Then pipeline steps are stored in pipeline_run.steps
    And trial burn lines in runway_calc.trial_burn_lines
    And catalyst candidates in catalyst_signal.candidates
    And peer group statistics in score_run.peer_group_stats

  Scenario: Metric views
    Given short-volume days and Form 4 rows are loaded
    Then short_volume_metric and insider_metric_daily return the same values as the stored-metric formulas
    And a missed day yields the earlier data with age_days and is_stale set

  Scenario: Query performance
    Given a loaded golden day
    When the screener query runs with default filters
    Then it returns in under 100 milliseconds
```

**Acceptance criteria**
- A5.1 Migrations apply and reverse cleanly on an empty database.
- A5.2 Every unique constraint and foreign key is enforced by the database.
- A5.3 Seeds load idempotently.
- A5.4 Units are documented in model docstrings or field help text.
- A5.5 Indexes exist for the screener and company page queries.
- A5.6 The three views exist and return correct results on the golden day and on loaded fixtures.

**Open items**
| Item | Default |
|---|---|
| One schema with app-prefixed tables (versus several schemas) | One schema; a separate restricted schema only when positions arrive |
| Array columns versus child tables for reason codes | PostgreSQL arrays; codes themselves are a list in code |
| `company_page` as a view or a materialised table | View first; materialise only if slow or if vintage logic gets awkward (O25) |

**Test plan**
| ID | Level | Test | Pass condition |
|---|---|---|---|
| T5.1 | Integration | Migrate up, down, up | Clean |
| T5.2 | Integration | Insert a duplicate for each unique key | Database error each time |
| T5.3 | Integration | Insert a second open ticker | Database error |
| T5.4 | Integration | Seed counts and idempotent re-run | As expected, no duplicates |
| T5.5 | Integration | Explain plan on the screener query over 1,053 rows | Uses indexes, under 100 ms |
| T5.6 | Unit | Factories create valid objects for every model | No errors |
| T5.7 | Integration | Metric views against a hand calculation on synthetic rows | Equal to the stored-metric formulas |
| T5.8 | Integration | Unknown reason code on a null | Rejected |

---

## Step 6: Golden-data loader

**Depends on:** Step 5. **Size:** S.

```gherkin
Feature: Load a golden day into the database

  Scenario: Load the golden export
    Given the golden export file
    When the loader runs
    Then companies and ticker history exist for all 1,053 rows
    And a finviz_snapshot exists per company for 2026-10-12
    And a runway_calc and catalyst_signal exist per company where the file has values
    And a source_call row with feed "fixture" records the origin

  Scenario: Runway export overlay
    Given the runway export
    When the overlay loader runs
    Then runway_calc rows gain cash, monthly burn, burn source and notes

  Scenario: Idempotent
    When the loader runs twice
    Then row counts are unchanged

  Scenario: Fixture data is never mistaken for production
    Then every loaded row is traceable to the fixture source_call
    And the application shows a visible "fixture data" banner when the latest run is a fixture run

  Scenario: Missing values
    Given a blank value in the export
    Then the stored value is NULL, never zero

  Scenario: Fields the golden export does not contain
    Given the golden export has no FINRA short volume, Form 4 or trial-list data
    When the loader runs
    Then those page fields are stored as NULL with the reason "fixture_not_available"
```

**Acceptance criteria**
- A6.1 1,053 companies and snapshots load.
- A6.2 Reading back the loaded values equals the file for every mapped column.
- A6.3 Re-running changes nothing.
- A6.4 Fixture rows are marked and the UI warns.
- A6.5 Every page field the export cannot supply is NULL with reason `fixture_not_available`.

**Open items**
| Item | Default |
|---|---|
| Page fields 15 to 17 (short volume), 34 to 39 (insider) and 52 to 55 (trial lists) are absent from the golden file | NULL with reason `fixture_not_available`; recorded fixtures F11 and F12 fill them in Steps 11A and 11B |
| The golden file lacks per-study lists and balance-sheet dates | Those columns stay NULL; the company page trial list is empty for fixtures |
| Whether the fixture files may be committed (they contain licensed Finviz-derived data) | Kept out of the repository; fetched from private storage in CI |

**Test plan**
| ID | Level | Test | Pass condition |
|---|---|---|---|
| T6.1 | Integration | Load, count rows | 1,053 in each table |
| T6.2 | Integration | Round-trip every column against the file | Equal |
| T6.3 | Integration | Load twice | No change |
| T6.4 | Integration | Blank cells | NULL |
| T6.5 | Integration | Overlay runway export | 1,040 rows with cash and burn |
| T6.6 | Integration | Page fields absent from the export | NULL with reason `fixture_not_available` for all 1,053 companies |

---

## Step 7: Score run service

**Depends on:** Steps 4, 5, 6. **Size:** M.

```gherkin
Feature: Scoring a day from stored data

  Scenario: Reproduce the golden day
    Given the golden day is loaded
    When a score run is executed for 2026-10-12 with the parity formula
    Then score_result holds 1,053 rows matching the stored scores

  Scenario: Records what it used
    Then each score_result stores the exact inputs dictionary, the components, and links to the
         runway_calc and catalyst_signal rows used
    And score_run.peer_group_stats holds the median, deviation and count for every group and dimension

  Scenario: Deterministic
    When the run is executed twice
    Then both runs produce identical score_result values

  Scenario: Atomic
    Given an error occurs mid-run
    Then no partial score_result rows are visible
    And the run is recorded as failed with the error

  Scenario: History is kept
    When a second run is executed for the same day
    Then the first run is kept and marked superseded
    And readers use the latest completed run
```

**Acceptance criteria**
- A7.1 All six score columns match the golden file for 1,053 rows.
- A7.2 Two runs give identical results.
- A7.3 A forced failure leaves no visible partial run.
- A7.4 A full-universe run completes in under 10 seconds.
- A7.5 `inputs` and `components` JSON are stored and non-empty for every row.

**Open items**
| Item | Default |
|---|---|
| Keep superseded runs forever | Yes, with optional pruning of non-candidate JSON after a retention window |

**Test plan**
| ID | Level | Test | Pass condition |
|---|---|---|---|
| T7.1 | Integration | Full golden regression through the database | 1,053 of 1,053 |
| T7.2 | Integration | Run twice and diff | Identical |
| T7.3 | Integration | Inject an exception at row 500 | No partial rows, failed status |
| T7.4 | Integration | Peer stats equal Step 1 intermediate values | Equal |
| T7.5 | Integration | Second run supersedes first | Reader returns latest |
| T7.6 | Integration | Timing | Under 10 seconds |

---

## Step 8: Screener service

**Depends on:** Step 7. **Size:** M.

```gherkin
Feature: Screening and the funnel

  Background:
    Given the default filters: market cap 75 to 1,000 (USD millions), price at least 1.00,
          average volume at least 50 (thousands of shares), shares outstanding at most 500 (millions),
          composite at least 5, outlier floor 0, include sparse groups true

  Scenario: Default screen on the golden day
    When the default screen runs
    Then the composite filter alone passes 339 companies
    And all filters together pass 139 companies

  Scenario Outline: Composite floor sensitivity
    Given all other default filters
    When the composite floor is <floor>
    Then <count> companies pass
    Examples:
      | floor | count |
      | 5.0 | 139 |
      | 5.5 | 88 |
      | 5.75 | 62 |
      | 6.0 | 46 |

  Scenario: Every failing filter is reported
    Given a company fails both the price and volume filters
    Then its excluded reasons list both

  Scenario: Missing values
    Given a company has no market cap
    Then it is excluded with reason "mcap_missing"

  Scenario: Ranking
    Then passing companies are ordered by composite descending, ties by ticker

  Scenario: Sparse groups excluded
    Given include sparse is false
    Then companies in sparse peer groups are excluded with reason "sparse_group"

  Scenario: Near the line
    Then a near-the-line list shows companies that fail exactly one filter,
         or pass all others and sit within 0.5 below the composite floor

  Scenario: Funnel events
    Given yesterday's and today's default screens
    Then funnel_event records each company that entered or left with the reason
```

**Acceptance criteria**
- A8.1 339 composite-only and 139 all-filters on the golden day.
- A8.2 The sensitivity table holds.
- A8.3 Every excluded company lists all failed filters.
- A8.4 Ad-hoc filters are computed from stored scores without recomputing scores.
- A8.5 Funnel events are correct for two synthetic consecutive days.

**Open items**
| Item | Default |
|---|---|
| Null handling for filter fields | Excluded, with `<field>_missing` (to confirm) |
| Near-the-line definition | As in the scenario, configurable |
| Composite floor default: 5 gives about 139; about 5.6 gives about 80 | 5, adjustable |
| Average volume unit confirmed as thousands | Assumed |

**Test plan**
| ID | Level | Test | Data | Pass condition |
|---|---|---|---|---|
| T8.1 | Integration | Default screen counts | golden day | 339 and 139 |
| T8.2 | Integration | Floor sensitivity | golden day | 139, 88, 62, 46 |
| T8.3 | Unit | Multi-failure reasons | synthetic | Both listed |
| T8.4 | Unit | Null field handling | synthetic | Reason codes as specified |
| T8.5 | Integration | Ordering and ties | golden day | Stable, deterministic |
| T8.6 | Integration | Funnel diff of two synthetic days | synthetic | Correct entered and left sets |
| T8.7 | Integration | Near-the-line list | golden day | Matches hand-computed sample |

---

## Step 9: Screener UI

**Depends on:** Step 8. **Size:** M.

```gherkin
Feature: Screener page

  Scenario: Default view
    Given a user is signed in
    When they open the screener
    Then the default filters are applied
    And the table shows the passing companies ranked by composite with ticker, company,
        industry, market cap, price, composite, Cash Health, Milestone, Outlier, peer group, next catalyst and runway

  Scenario: Live counts
    When the user moves any filter
    Then the count and table update within 500 milliseconds without a full page reload

  Scenario: Why excluded
    Given the user searches for an excluded ticker
    Then the page lists every filter it failed with the actual value and the threshold

  Scenario: Saved screens
    When the user saves the current filters under a name
    Then they can reload it later

  Scenario: Funnel and near the line
    Then the user can open a funnel log showing who entered and left, and a near-the-line list

  Scenario: Data freshness
    Given the latest score run is more than 3 days old
    Then a banner states the age

  Scenario: Authentication
    Given an anonymous visitor
    Then every page redirects to sign-in
```

**Acceptance criteria**
- A9.1 On the golden day the default view shows 139 companies.
- A9.2 Filter changes update through partial page updates in under 500 ms on the golden day.
- A9.3 Why-excluded works for any ticker in the universe.
- A9.4 Saved screens persist.
- A9.5 All pages require sign-in.
- A9.6 Table export to CSV is available (nice to have, confirm).
- A9.7 The table and the CSV export read only the `company_page` read model.

**Open items**
| Item | Default |
|---|---|
| Table library | A small JavaScript table library with HTMX partials |
| Saved screens per user or shared | Shared, with owner recorded |
| CSV export | Include |

**Test plan**
| ID | Level | Test | Pass condition |
|---|---|---|---|
| T9.1 | Integration | Client request with default filters | 139 rows |
| T9.2 | Integration | Partial endpoint for each filter change | Correct counts, under 500 ms |
| T9.3 | Integration | Why-excluded for 5 sample tickers | Reasons and values correct |
| T9.4 | Integration | Save and reload a screen | Equal filters |
| T9.5 | Integration | Anonymous access | Redirect |
| T9.6 | E2E | Browser test of slider, table and drawer | Passes |
| T9.7 | Manual | Usability checklist with two users | Signed off |

---

## Step 10: Finviz ingestion

**Depends on:** Step 5. **Size:** M.

```gherkin
Feature: Daily Finviz snapshot

  Scenario: Download and store
    Given a valid Finviz Elite token
    And the export URL uses view 151 and the filter f=sec_healthcare, both read from settings
    When the market stage runs
    Then the CSV is downloaded, stored unchanged in object storage with its hash,
         and a source_call row records the result and the filter used

  Scenario: Universe definition changes are visible
    Given the sector filter or view number in settings is changed
    Then the change is recorded and the next data-quality check compares the row count to the previous run

  Scenario Outline: Parsing
    Given a cell <raw>
    Then it is stored as <stored>
    Examples:
      | raw | stored |
      | "12.4%" | 12.4 |
      | "-" | NULL |
      | "$1,234.5M" | 1234.5 |
      | "" | NULL |

  Scenario: Column mapping
    Then each CSV header maps to its internal key per the Finviz column map
    And company, sector, industry and earnings date are kept as text
    And the earnings date is also parsed into a date when it matches M/D/YYYY

  Scenario: All requested columns are stored
    Given the export requests the 50 columns listed in the Finviz column map
    Then every column is stored on finviz_snapshot, including those the scores do not use

  Scenario: Duplicate headers
    Given the 52-week high and low columns each appear twice under the same header
    Then the columns are read by position, not by header name

  Scenario: Companies and tickers
    Given a ticker not seen before
    Then a company and an open ticker_history row are created
    And given a known company whose industry changed, the change is recorded
    And given a company absent from today's file, its last_seen_date is not advanced

  Scenario: Whole-universe blank column
    Given a column is blank for every company
    Then a data_quality_check fails for that column
    And an alert is raised

  Scenario: Row count anomaly
    Given today's row count differs from the previous run by more than 20 percent
    Then a data_quality_check fails

  Scenario: Re-download of identical content
    Given the file hash equals a stored hash for the same date
    Then no duplicate snapshot rows are created

  Scenario: Download failure
    Given the download fails after three attempts
    Then the stage stops, the failure is recorded, and an alert is raised
```

**Acceptance criteria**
- A10.1 A raw sample file parses to the expected rows with correct units.
- A10.2 `-` and blanks become NULL, never zero.
- A10.3 A blank column across the universe fails a check (the `eps_this` case).
- A10.4 Identical re-downloads are idempotent.
- A10.5 Failure stops Stage A and alerts.
- A10.6 The token is read from a secret and never logged.
- A10.7 All 50 columns are stored for every row; the duplicate headers are resolved by position.

**Open items**
| Item | Default |
|---|---|
| A raw Finviz CSV sample (F6) | The engineer downloads one |
| The two Finviz columns both headed "52-Week High" and "52-Week Low" | `w52h` and `w52l` store percent distance |
| Finviz terms on storing daily history and on AI processing | To be checked before Phase 4; storage proceeds |
| How ADRs and foreign listings are identified | Name patterns plus country field when present |

**Test plan**
| ID | Level | Test | Data | Pass condition |
|---|---|---|---|---|
| T10.1 | Unit | Cell parsing table | synthetic | Exact |
| T10.2 | Contract | Full parse of the raw CSV | F6 | Row and column counts match; spot-check 10 tickers |
| T10.3 | Integration | Blank column check | F6 with a column blanked | Check fails, alert queued |
| T10.4 | Integration | Row count anomaly | synthetic | Fails at 21 percent change |
| T10.5 | Integration | Same file twice | F6 | No duplicate rows |
| T10.6 | Integration | Forced HTTP failure | mocked | Three attempts, stage stops |
| T10.7 | Unit | Log scan | CI | Token never appears |
| T10.8 | Contract | All 50 columns stored | F6 | 50 of 50 columns present for every row |
| T10.9 | Unit | Duplicate headers | synthetic header row | Values land in the correct positional columns |

---

## Step 11: SEC collectors

**Depends on:** Step 5. **Size:** L.

```gherkin
Feature: SEC EDGAR collection

  Scenario: Declared user agent
    Then every SEC request sends the configured User-Agent

  Scenario: Throttling
    When many requests are made across companyfacts, submissions and Form 4 documents
    Then one shared limiter keeps consecutive requests at least 110 milliseconds apart
    And no collector has a limiter of its own

  Scenario: Retries
    Given a 429 or 5xx response
    Then the request is retried up to three times with exponential backoff
    And the final outcome is recorded in source_call

  Scenario: Ticker to CIK
    Given company_tickers.json
    Then each ticker resolves to a ten-digit CIK, cached for the day
    And a ticker with no CIK is recorded as skipped with reason "no_cik"

  Scenario: Filing discovery
    Given the submissions feed for a CIK
    Then 10-K, 10-Q, 20-F, 6-K and their amendments are stored in sec_filing
    And a new filing since the last run marks the company for a facts refresh
    And new Form 4 entries are handed to the Form 4 collector (Step 11B)

  Scenario: Facts refresh
    Given a company with a new filing or no stored facts
    When companyfacts is fetched
    Then only allow-listed tags are stored in sec_fact with accession, filed date and period
    And us-gaap is read first and ifrs-full only if us-gaap has nothing
    And only USD values are stored, otherwise the reason is "non_usd_only"

  Scenario: Unchanged payload
    Given the response hash equals the stored hash
    Then nothing is reloaded

  Scenario: Failure isolation
    Given one company's fetch fails
    Then other companies continue and the failure is recorded with a reason
```

**Acceptance criteria**
- A11.1 Recorded responses for the six sample cases load as specified.
- A11.2 Throttle and retry behaviour are covered by tests with a fake clock.
- A11.3 Amended filings are detected.
- A11.4 Reloading the same payload creates no duplicate facts.
- A11.5 No company failure stops the run.
- A11.6 Facts, submissions and Form 4 requests share one limiter; a test interleaves all three and still sees gaps of at least 110 ms.

**Open items**
| Item | Default |
|---|---|
| Contact details for the User-Agent | A shared team address supplied through configuration |
| SEC nightly bulk archives instead of per-ticker calls | Per-ticker with change detection first; evaluate bulk if volume requires |
| Final allow-list of tags (cash, short-term investments, operating cash flow, R&D, revenue) | Tags from the other system's documented lists |
| Run from the production IP; the analyst's shared IP was blocked | Record fixtures from the production host |

**Test plan**
| ID | Level | Test | Data | Pass condition |
|---|---|---|---|---|
| T11.1 | Unit | Throttle with fake clock | synthetic | Gap at least 110 ms |
| T11.2 | Unit | Retry and backoff | mocked 429, 503 | Three attempts then failure recorded |
| T11.3 | Contract | Six sample companies | F7 | Filings and facts as expected |
| T11.4 | Contract | IFRS filer uses ifrs-full tags | F7 | Facts stored under ifrs-full |
| T11.5 | Contract | Non-USD filer | F7 | No facts, reason `non_usd_only` |
| T11.6 | Contract | 10-K/A detection | F7 | `is_amendment` true |
| T11.7 | Integration | Reload same payload | F7 | No new rows |
| T11.8 | Integration | One failing company | mocked | Others complete |
| T11.9 | Unit | Shared limiter across three request types | fake clock | Gap at least 110 ms between any two requests |

---

## Step 11A: FINRA short-volume collector and metrics

**Depends on:** Step 5. **Size:** S. Part of the universe-wide daily batch (Stage B).

```gherkin
Feature: FINRA Reg SHO daily short volume

  Scenario: Daily file
    Given the FINRA consolidated short-volume file for a trading date
    When the filings stage runs
    Then the file is downloaded once, stored unchanged in object storage with its hash,
         and a source_call row records the result

  Scenario: First-run backfill
    Given no short-volume history exists
    Then the files for the previous 45 calendar days are fetched

  Scenario: Non-trading days
    Given FINRA has no file for a date (weekend or market holiday)
    Then the date is recorded as "no_file_for_date" and is not treated as a failure

  Scenario: Universe filter
    Then only rows for universe tickers are stored in short_volume_daily
    And a universe ticker missing from a day's file gets reason "not_in_finra_file"

  Scenario: Latest ratio
    Given 600 short volume and 1,000 total volume on the latest trading day with data
    Then latest_ratio is 0.6000 (stored as 0 to 1 at four decimals)

  Scenario Outline: Trend and change
    Given the mean daily ratio over the last 5 trading days is <avg5>
    And the mean daily ratio over the last 30 trading days is <avg30>
    Then delta_pts is <delta> and the trend is <trend>
    Examples:
      | avg5 | avg30 | delta | trend |
      | 0.55 | 0.50 | 5.0 | rising |
      | 0.48 | 0.50 | -2.0 | falling |
      | 0.50 | 0.50 | 0.0 | flat |

  Scenario: The 30-day window includes the 5 recent days
    Then the 30-day mean is taken over the last 30 trading days, including the latest 5

  Scenario: Insufficient history
    Given fewer than 5 trading days of data
    Then the metrics are null with reason "insufficient_history"

  Scenario: Failure and carry-forward
    Given the download fails after three attempts
    Then the failure is recorded
    And the short_volume_metric view uses the latest stored trading days
    And age_days reports how old they are and is_stale is true after the maximum age

  Scenario: Metrics are a view
    Then short_volume_metric is a database view over short_volume_daily, not a stored table
    And the same SQL runs as a function of as_of_date for past dates

  Scenario: Idempotent
    When the same date is loaded twice
    Then no duplicate rows are created
```

**Acceptance criteria**
- A11A.1 Recorded files for three consecutive trading days produce the expected rows and metrics.
- A11A.2 Weekend and holiday dates are recorded without raising a failure.
- A11A.3 Ratio, delta and trend match the formulas (delta to 1e-9).
- A11A.4 Reloading a date creates no duplicates.
- A11A.5 A failure here never blocks scoring.
- A11A.6 The first run fetches 45 calendar days of files.
- A11A.7 The page label says "short volume, not short interest".
- A11A.8 The metric view returns the same results as the formulas for any past date.

**Open items**
| Item | Default |
|---|---|
| When FINRA publishes each day's file | Fetch in Stage B (23:30 ET); a missing file is retried on the next run |
| Ticker format differences between FINRA and Finviz (share classes, punctuation) | Normalise both sides; unmatched tickers get `not_in_finra_file` |
| Maximum carry-forward age | 5 trading days |
| Whether to store the short-exempt volume | Stored, not displayed |

**Test plan**
| ID | Level | Test | Data | Pass condition |
|---|---|---|---|---|
| T11A.1 | Contract | Parse three consecutive daily files | F11 | Rows and totals match the files |
| T11A.2 | Contract | Weekend date has no file | F11 | `no_file_for_date`, no failure |
| T11A.3 | Unit | Ratio, delta and trend table | table above | Exact (delta approx 1e-9) |
| T11A.4 | Unit | Window includes the latest 5 days | synthetic 30-day series | Matches hand calculation |
| T11A.5 | Integration | Load the same date twice | F11 | No duplicate rows |
| T11A.6 | Integration | Insufficient history | 4 days of data | Nulls with reason |
| T11A.7 | Integration | Download failure then recovery | mocked | Carried with age, then fresh |
| T11A.8 | Integration | Ticker missing from a day's file | F11 | `not_in_finra_file` |
| T11A.9 | Integration | View as of a past date versus the formulas | synthetic 30-day series | Equal |

---

## Step 11B: Form 4 insider-activity collector and metrics

**Depends on:** Steps 5 and 11 (SEC client, shared rate limiter, submissions). **Size:** L. Part of the universe-wide daily batch (Stage B).

```gherkin
Feature: SEC Form 4 insider activity

  Scenario: Discover filings
    Given the submissions feed for a company
    Then every Form 4 filed in the last 90 days that is not yet stored is queued for fetching

  Scenario: Fetch and parse
    When a Form 4 XML document is fetched
    Then the filing is stored with its date and its 10b5-1 flag
    And each non-derivative transaction is stored with reporting owner, officer and director flags,
        date, code, shares, price per share and acquired or disposed
    And amount_usd is shares times price, or null when either is missing

  Scenario: Parse failure
    Given a document that cannot be parsed
    Then the filing is stored with parse_status "failed" and its reason
    And it is excluded from every count
    And a data-quality check reports the number of failures

  Scenario: One shared rate limiter
    Given requests to submissions, company facts and Form 4 documents are interleaved
    Then any two SEC requests are at least 110 milliseconds apart

  Scenario: Worked example of the metrics
    Given these Form 4 filings in the last 90 days for one company:
      | accession | owner | role | code | shares | price | under 10b5-1 |
      | A1 | CEO | officer | P | 1000 | 5.00 | no |
      | A2 | CFO | officer | S | 2000 | 6.00 | yes |
      | A3 | Dir1 | director | S | 1000 | 6.00 | no |
      | A4 | Dir1 | director | S | 500 | 7.00 | no |
    Then filings_count is 4
    And open_market_buys_count is 1 and open_market_sales_count is 3
    And officers_directors_buying_count is 1 and officers_directors_selling_count is 2
    And pct_10b5_1_dollars is 55.81 (12,000 of 21,500 sale dollars)

  Scenario: Other transaction codes
    Given a transaction with code G (gift)
    Then it is stored but counted as neither a buy nor a sale

  Scenario: Rolling window
    Given a filing dated 90 days ago and another dated 91 days ago
    Then only the first is counted

  Scenario: No insider filings
    Given a company with no Form 4 in 90 days
    Then the counts are 0 and pct_10b5_1_dollars is null, with no error

  Scenario: No open-market sales
    Given only purchases in the window
    Then pct_10b5_1_dollars is null

  Scenario: First-run backfill
    Given no Form 4 history exists
    Then filings from the last 90 days are fetched for every company in the universe

  Scenario: Incremental runs
    Given a filing is already stored
    Then it is not fetched again

  Scenario: Failure and carry-forward
    Given the SEC feed fails for a company
    Then other companies continue
    And the insider_metric_daily view uses the stored filings, with age_days and is_stale set after the maximum age

  Scenario: Metrics are a view
    Then insider_metric_daily is a database view over sec_form4_filing and sec_form4_transaction, not a stored table
    And the same SQL runs as a function of as_of_date for past dates
```

**Acceptance criteria**
- A11B.1 The worked example gives exactly the stated metrics.
- A11B.2 Recorded filings (purchase, planned sale, unplanned sale, multi-owner, gift, amendment, unparseable) parse as specified.
- A11B.3 The window includes day 90 and excludes day 91.
- A11B.4 Re-running fetches only new accession numbers and creates no duplicates.
- A11B.5 One shared limiter covers all SEC calls.
- A11B.6 A parse failure never breaks a run and is visible.
- A11B.7 A failure here never blocks scoring.
- A11B.11 The metric view returns the stated results for the worked example at any as_of_date.

**Open items**
| Item | Default |
|---|---|
| Whether a 4/A replaces its original | Yes: the amendment replaces the original |
| Whether derivative-table transactions count | No: non-derivative only |
| Which filings belong to a company (issuer versus reporting owner) | Filings where the company is the issuer |
| 10b5-1 checkbox on filings made before it existed | Treated as not 10b5-1 |
| Volume and first-run backfill time for about 1,053 companies | To be measured on the first run |
| Maximum carry-forward age | 7 days |

**Test plan**
| ID | Level | Test | Data | Pass condition |
|---|---|---|---|---|
| T11B.1 | Contract | Parse each recorded document type | F12 | Fields as expected |
| T11B.2 | Unit | Worked example of the metrics | table above | Exact (pct to 1e-2) |
| T11B.3 | Unit | Window boundary at 90 and 91 days | synthetic dates | 90 counted, 91 not |
| T11B.4 | Unit | Codes other than P and S | synthetic | Stored, not counted |
| T11B.5 | Unit | No filings and no sales | synthetic | Zeros and null percentage |
| T11B.6 | Integration | Re-run with stored filings | F12 | No refetch, no duplicates |
| T11B.7 | Unit | Shared rate limiter across facts, submissions and Form 4 | fake clock | Gap at least 110 ms |
| T11B.8 | Integration | Unparseable document | F12 | Failed status, excluded, check raised |
| T11B.9 | Integration | One failing company | mocked | Others complete |
| T11B.10 | Integration | Amendment replaces original | F12 | Single effective filing |
| T11B.11 | Integration | View as of a past date versus the worked example | synthetic | Equal |

---

## Step 12: Runway calculator

**Depends on:** Step 11 for the integration tests only. **Size:** M.

```gherkin
Feature: Runway

  Scenario: Basic runway
    Given cash of 100 million USD and monthly burn of 5 million USD
    Then runway is 20 months

  Scenario Outline: Cap at 360 months
    Given cash <cash> and monthly burn <burn>
    Then runway is <runway> and capped is <capped> with reason <reason>
    Examples:
      | cash | burn | runway | capped | reason |
      | 100 | 0 | 360 | true | zero_burn |
      | 100 | 0.2 | 360 | true | tiny_burn |
      | 100 | 5 | 20 | false | none |

  Scenario: Operating burn from the annual figure
    Given an annual operating cash flow outflow of 60 million and no other burn
    Then operating burn is 5 million per month

  Scenario: Positive operating cash flow
    Given operating cash flow is positive
    Then operating burn is 0 and the note "company_cash_positive" is stored

  Scenario: Trial burn
    Given active recruiting trials of 2 Phase 1, 3 Phase 2 and 1 Phase 3
    Then trial burn per month is 2*150K + 3*800K + 1*2.5M
    And runway_calc.trial_burn_lines holds one entry per phase with the count, rate and amount

  Scenario: Total burn
    Then total burn equals operating burn plus trial burn

  Scenario: KRYS example
    Given cash of 845.6 million, zero operating burn and 11 matched trials giving 6.45 million per month
    Then runway is 131.1 months and is not capped

  Scenario: R&D fallback
    Given operating cash flow is missing and R&D expense is available
    Then burn comes from R&D expense and the reason "fallback_rd_expense" is stored

  Scenario: Foreign filer without usable data
    Given no XBRL data is available but trials are active
    Then trial burn is stored, cash is null, runway is null,
         and the reasons include "phase_burn_only_no_xbrl"

  Scenario: Cash and short-term investments
    Given cash and short-term investments have the same balance-sheet date
    Then liquidity is their sum
    But given the dates differ
    Then liquidity is cash only

  Scenario: Year-to-date cash flow
    Given a company with no annual filing and the switch ocf_ytd_naive_sum on
    Then the old naive quarterly sum is used
    And with the switch off, year-to-date facts are de-cumulated first

  Scenario: Financing date
    Then financing_date equals the balance-sheet date plus runway months times 30.44 days

  Scenario Outline: Derived display values
    Given runway of <months> months
    Then runway in quarters is <quarters> and the rating is <rating>
    Examples:
      | months | quarters | rating |
      | 5.0 | 1.7 | Critical |
      | 11.9 | 4.0 | Short |
      | 20.0 | 6.7 | Moderate |
      | 131.1 | 43.7 | Long |

  Scenario: Cash-flow-positive display
    Given a company that is cash-flow positive
    Then runway displays blank with the label "cash-flow positive"

  Scenario: Reproduce the runway export
    Given the runway export
    Then cash divided by burn, capped at 360, equals the reported runway for all 1,040 rows with both values
```

**Acceptance criteria**
- A12.1 The arithmetic equals the export for 1,040 rows.
- A12.2 Every input (cash, tags, periods, burns, source, flags) is stored on `runway_calc`.
- A12.3 Every null has a reason code.
- A12.4 Rates are read from configuration.
- A12.5 With the YTD switch off, a synthetic cumulative series yields the correct discrete burn.
- A12.6 Quarters equal round(months/3, 1); the rating is Critical below 6, Short below 12, Moderate below 24, otherwise Long; both are derived at read time and never stored.

**Open items**
| Item | Default |
|---|---|
| Which trial statuses count as active for trial burn | RECRUITING only |
| How multi-phase studies are rated | Highest phase |
| Exact R&D fallback rule (period and conversion) | Annual R&D expense divided by 12 |
| Annual versus latest operating cash flow | Annual when an annual filing exists |
| What revenue and R&D tags feed beyond the fallback | Stored, not used in scoring |
| Display of a capped runway (360 months) | Shown as "360+ months" with the cap flag (to confirm) |
| Burn breakdown samples for KTTA, SMMT, RZLT, APLM, PACS, ABBV, APMD, ABVX | Requested from the other system's owner |

**Test plan**
| ID | Level | Test | Data | Pass condition |
|---|---|---|---|---|
| T12.1 | Unit | Basic runway and caps | table above | Exact |
| T12.2 | Unit | Trial burn lines | synthetic counts | Exact amounts |
| T12.3 | Unit | KRYS example | given values | 131.1 months |
| T12.4 | Unit | Fallback paths and reason codes | synthetic | As scenarios |
| T12.5 | Unit | YTD de-cumulation, switch on and off | synthetic cumulative facts | Distinct, correct results |
| T12.6 | Integration | Arithmetic on the export | F2 | 1,040 of 1,040 |
| T12.7 | Contract | Facts for about 10 tickers vs the export | F7 and F2 | Within 1 percent where the formula is known |
| T12.8 | Unit | Quarters and rating boundaries (5.99, 6, 11.99, 12, 23.99, 24) | table | Exact |
| T12.9 | Unit | Cash-flow-positive display | synthetic | Blank with label |

---

## Step 13: ClinicalTrials.gov collector

**Depends on:** Step 5. **Size:** M.

```gherkin
Feature: ClinicalTrials.gov collection

  Scenario: Query by sponsor aliases
    Given a company with sponsor aliases
    When studies are fetched
    Then the v2 API is queried with each active alias and all pages are read

  Scenario: Matching
    Given the returned studies
    Then a study is attached to a company when its lead sponsor matches an alias
         with similarity at or above the configured threshold
    And manual overrides always win
    And the match score is stored

  Scenario: Filters
    Then observational studies are excluded
    And completed studies whose completion date is more than 730 days ago are excluded
    And studies in any other status are kept, including terminated, withdrawn and suspended

  Scenario: Date precision
    Given a month-only completion date
    Then the raw text is kept, the date is stored as the 1st, and precision is "month"

  Scenario: Change-only versions
    Given a study's content is unchanged since the last run
    Then no new ct_study_version row is written
    But given its status or dates changed
    Then a new version row is written

  Scenario: Politeness and retries
    Then requests are spaced by a configured delay and retried up to three times

  Scenario: Failure isolation
    Given one sponsor query fails
    Then other sponsors continue and the failure is recorded
```

**Acceptance criteria**
- A13.1 Recorded responses for FDMT, DFTX, KTTA, APLM and SMMT produce the expected study sets.
- A13.2 Alias thresholds and overrides behave as specified.
- A13.3 Month-only parsing matches the rules.
- A13.4 Unchanged content writes no rows.
- A13.5 No sponsor failure stops the run.

**Open items**
| Item | Default |
|---|---|
| Similarity metric and threshold | A standard token-based ratio, threshold 0.6, SEC former names as extra aliases |
| Which statuses to request (the live export includes terminated, withdrawn and suspended) | Request all statuses |
| 730-day completed rule | As documented in the fields file, to verify |
| Retention of conditions and interventions | Stored |

**Test plan**
| ID | Level | Test | Data | Pass condition |
|---|---|---|---|---|
| T13.1 | Contract | Five sample companies | F5 or recorded | Study sets match |
| T13.2 | Unit | Alias match scores and overrides | synthetic names | As specified |
| T13.3 | Unit | Date parsing | table | Exact |
| T13.4 | Integration | Same payload twice, then a changed status | synthetic | 0 new rows, then 1 |
| T13.5 | Unit | Retry and delay | fake clock | As specified |
| T13.6 | Integration | One failing sponsor | mocked | Others complete |

---

## Step 13A: Trial display derivations

**Depends on:** Step 13 (stored study versions). **Size:** S. Pure read-time functions; nothing is stored.

```gherkin
Feature: Trial panel values derived from stored studies

  Scenario: Next-milestone ordering
    Given a company's studies with completion dates
    Then the next-milestone card shows the Phase 3 study first, otherwise the study with the soonest completion
    And past completion dates are not filtered out

  Scenario Outline: Quarters out
    Given a completion date <days> days from today
    Then the card shows <display>
    Examples:
      | days | display |
      | 182 | 2 quarters out |
      | 20 | 1 quarter out |
      | 0 | Past due |
      | -40 | Past due |

  Scenario: Drug name
    Given a study whose first intervention is "DRUG: Zimura"
    Then the drug is "Zimura" with the type prefix removed

  Scenario: Pipeline total and phase chips
    Given 7 studies across Phase 1, Phase 2 and Phase 3
    Then the pipeline shows 7 and one chip for each distinct phase

  Scenario: Indications
    Given studies list the same condition more than once
    Then the condition is not de-duplicated, matching the current page

  Scenario: Upcoming completions
    Given studies with primary or study completion dates
    Then those with a completion date on or after today are listed using the primary date, else the study date
    And a month-only date counts as the first of the month
```

**Acceptance criteria**
- A13A.1 Ordering, quarters, drug name, chips, indications and upcoming completions equal the specified results for the recorded studies of FDMT, DFTX, KTTA, APLM and SMMT.
- A13A.2 The functions take stored studies and a `today` value, with no hidden clock.
- A13A.3 A company with no studies yields empty values with reason `no_trials`.

**Open items**
| Item | Default |
|---|---|
| Where a combined "Phase 2/3" study falls in the Phase 3 first ordering | Treated as Phase 3 (to confirm) |
| Indications are not de-duplicated | Parity with the current page; de-duplicate later if the product owner wants it |

**Test plan**
| ID | Level | Test | Data | Pass condition |
|---|---|---|---|---|
| T13A.1 | Unit | Ordering with and without a Phase 3 study | synthetic | Phase 3 first, then soonest |
| T13A.2 | Unit | Quarters out boundaries (0, 1, 45, 46, 91 days) | table | Exact, minimum 1 |
| T13A.3 | Unit | Drug prefix stripping | synthetic interventions | Exact |
| T13A.4 | Unit | Chips and total | synthetic | Distinct phases only |
| T13A.5 | Unit | Upcoming completions with month-only dates | synthetic | First of month, filtered by today |
| T13A.6 | Contract | Five sample companies | F5 | Match the specified results |

---

## Step 14: Catalyst derivation

**Depends on:** Step 13 for the integration test. **Size:** M.

```gherkin
Feature: Clinical catalyst date

  Background:
    Given studies as of the run date

  Scenario: Eligible studies
    Then a study is eligible when it is interventional, lists Phase 2 or Phase 3
         (Phase 1/2 counts as Phase 2), and has status recruiting, not yet recruiting,
         enrolling by invitation or active not recruiting

  Scenario: Date selection
    Given each eligible study's primary completion date (else study completion date)
    And month-only dates become the 15th
    And dates more than 180 days in the past are ignored
    Then the catalyst is the earliest remaining date, with the study's highest phase

  Scenario: Overdue
    Given the chosen date is in the past
    Then the overdue flag is true

  Scenario: No candidate
    Then the clinical date is empty and next_event_source is "financing" if a runway-based date exists, else "none"

  Scenario: Candidates are explained
    Then every considered study is stored in catalyst_signal.candidates with eligible, reject reason and chosen

  Scenario: Active counts
    Then active Phase 2 and Phase 3 counts follow the documented status set

  Scenario: Sample reproduction
    Given the 79 sampled tickers with cached studies
    Then at least 71 reproduce the golden clinical date and phase
```

**Acceptance criteria**
- A14.1 At least 71 of 79 sampled tickers match; the target is all 79 once the rule is finalised.
- A14.2 Every decision is traceable through `catalyst_signal.candidates`.
- A14.3 The rule version is stored with each signal.

**Open items**
| Item | Default |
|---|---|
| The 8 differing tickers (GPCR, DFTX, REGN, MLTX, BMY, EYPT, KRYS, MBOT) and the true rule | Rule v0 above |
| Tie-break between studies with the same date | Phase 3 first |
| "Active" definition conflict: the fields file counts NOT_YET_RECRUITING; the XBIT data does not | Excluded, matching the data |
| Whether the 180-day window applies to all statuses | All statuses |

**Test plan**
| ID | Level | Test | Data | Pass condition |
|---|---|---|---|---|
| T14.1 | Unit | Eligibility table | synthetic studies | As specified |
| T14.2 | Unit | Month-only and window boundaries (179, 180, 181 days) | synthetic | Exact |
| T14.3 | Unit | Tie-break and no-candidate cases | synthetic | As specified |
| T14.4 | Contract | 79-ticker reproduction | F5 and F1 | At least 71 |
| T14.5 | Integration | Candidate rows stored | sample | One row per study |

---

## Step 15: Effective-input resolver and carry-forward

**Depends on:** Steps 5, 11A, 11B, 12, 14. **Size:** S.

```gherkin
Feature: Choosing the inputs the scorer sees

  Scenario Outline: Resolution
    Given the latest runway or catalyst value for a company
    Then the scorer receives <result>
    Examples:
      | situation | result |
      | fetched this cycle | the fresh value, age 0 |
      | fetch failed, last good value within max age | the last good value, carried_forward true, age in days |
      | fetch failed, last good value older than max age | NULL with reason "stale_expired" |
      | never obtained | NULL with its original reason |

  Scenario: Maximum ages
    Then the defaults are 120 days for SEC-derived runway and 7 days for ClinicalTrials.gov-derived data
    And 5 trading days for FINRA short-volume metrics and 7 days for Form 4 insider metrics
    And they are editable settings

  Scenario: Finviz is never carried forward
    Given Finviz fields for a company are missing today
    Then they stay NULL

  Scenario: Visibility
    Then score_result lists every carried and missing input with its age and reason

  Scenario: Parity switch
    Given carry_forward is off
    Then a failed fetch yields NULL, as in the original system
```

**Acceptance criteria**
- A15.1 Every row of the resolution table is covered by a test.
- A15.2 Ages are exact day counts.
- A15.3 The parity switch reproduces the original behaviour.

**Open items**
| Item | Default |
|---|---|
| Final max ages | 120 days, 7 days, 5 trading days and 7 days |
| Whether a carried value older than half its max age triggers a warning | Yes, on the health page |

**Test plan**
| ID | Level | Test | Pass condition |
|---|---|---|---|
| T15.1 | Unit | Table-driven resolution | Exact |
| T15.2 | Unit | Boundary at max age and max age plus 1 | Carried, then expired |
| T15.3 | Integration | Score result lists carried inputs | Present with ages |
| T15.4 | Integration | Parity switch on a failure day | Null input |
| T15.5 | Unit | Short-volume (5 trading days) and insider (7 days) boundaries | Carried, then expired |

---

## Step 16: Pipeline orchestration

**Depends on:** Steps 10 to 15, 11A and 11B. **Size:** L.

```gherkin
Feature: Daily pipeline

  Scenario: Market stage
    When "run_stage market" executes
    Then it downloads Finviz, parses, runs quality checks, resolves inputs, scores,
         applies the default screen and writes funnel events, in that order

  Scenario: Filings stage
    When "run_stage filings" executes
    Then it resolves CIKs, checks submissions for filings and Form 4, refreshes facts where needed,
         fetches and parses new Form 4 documents, fetches new FINRA short-volume files,
         queries trials, computes runway, insider and short-volume metrics, and derives catalyst signals

  Scenario: No overlap
    Given a stage is running
    Then a second start of the same stage exits without work

  Scenario: Failure isolation
    Given the SEC feed is down
    Then the filings stage continues with trials and records the SEC failures
    And the market stage still scores using carried-forward inputs

  Scenario: Insider and short-volume failures never block scoring
    Given the FINRA download or Form 4 collection fails
    Then the filings stage continues and records the failure
    And the market stage scores normally
  Scenario: Finviz failure
    Given the Finviz download fails
    Then the market stage stops and alerts

  Scenario: Resumable and idempotent
    Given a stage failed at a step
    When it is re-run for the same date
    Then completed steps are not duplicated and the stage resumes

  Scenario: Dry run
    When a stage runs in dry-run mode against recorded fixtures
    Then no external call is made and the full flow completes

  Scenario: Run record
    Then pipeline_run (with its steps) and source_call rows describe every execution
```

**Acceptance criteria**
- A16.1 Both stages complete against fixtures in dry-run mode.
- A16.2 Injected failures follow the rules above.
- A16.3 Re-running a day produces no duplicates.
- A16.4 A lock prevents overlapping runs.
- A16.5 The filings stage finishes within its time budget for 1,053 companies, including Form 4 and FINRA.
- A16.6 Insider and short-volume failures never block scoring.

**Open items**
| Item | Default |
|---|---|
| Schedule times | Market about 16:30 ET, filings about 23:30 ET |
| Market stage on market holidays | Skipped |
| Time budget for the filings stage | 3 hours, to confirm after measuring |

**Test plan**
| ID | Level | Test | Pass condition |
|---|---|---|---|
| T16.1 | E2E | Dry-run both stages | Complete, correct row counts |
| T16.2 | E2E | Fault injection per source | Behaviour as scenarios |
| T16.3 | Integration | Re-run after mid-stage failure | Resumes, no duplicates |
| T16.4 | Integration | Concurrent start | Second exits |
| T16.5 | Manual | Timed run in staging | Within budget |

---

## Step 17: Health page and alerts

**Depends on:** Step 16. **Size:** S.

```gherkin
Feature: Pipeline health

  Scenario: Health page
    Then it shows the last run per stage, last success per source, quality-check results,
         counts of carried and null inputs, and run history

  Scenario Outline: Alerts
    Given <condition>
    Then an email is sent once
    Examples:
      | condition |
      | the market stage fails |
      | a source fails on two consecutive nights |
      | a column is blank across the whole universe |
      | the Finviz row count changes by more than 20 percent |
      | Form 4 parse failures exceed the configured threshold for a run |
      | FINRA is missing a file for a trading day |

  Scenario: De-duplication
    Given the same condition persists
    Then no further email is sent until it clears and recurs
```

**Acceptance criteria**
- A17.1 Seeded failures appear on the page.
- A17.2 Each alert condition sends exactly one email.
- A17.3 Recovery clears the alert state.

**Open items**
| Item | Default |
|---|---|
| Alert channel and recipients | Email to the team |
| Mail provider | A low-cost transactional mail service |

**Test plan**
| ID | Level | Test | Pass condition |
|---|---|---|---|
| T17.1 | Integration | Seeded failures on the page | Shown |
| T17.2 | Integration | Each alert condition | One email |
| T17.3 | Integration | Persisting condition | No repeat |
| T17.4 | Integration | Recovery then recurrence | Email again |
| T17.5 | Integration | Form 4 failure threshold and a missing FINRA trading-day file | One email each |

---

## Step 18: Company page

**Depends on:** Step 7, and Steps 11A and 11B for the two new panels (fixtures suffice). **Size:** M.

```gherkin
Feature: Company page

  Scenario: Header and chart
    When a user opens /company/ABEO
    Then the page shows the ticker, company, industry, peer group and data date
    And a TradingView chart widget for the symbol

  Scenario Outline: Metric tiles
    Given the latest snapshot
    Then the tile <tile> displays <display>
    Examples:
      | tile | display |
      | Price | $5.39 |
      | Market cap | $308M |
      | 52-wk from high | -29.61% |
      | Short float | 29.92% |
      | Inst. ownership | 69.11% |
      | Cash/share | $2.57 |
      | Earnings date | 8/13/2026 8:30:00 AM |

  Scenario: Unavailable tiles
    Then the fair value, bear/bull and upside tiles show "not yet available"

  Scenario Outline: Display formats
    Given a stored value <value> for <field>
    Then it displays as <display>
    Examples:
      | field | value | display |
      | Market cap | 307.94 | $308M |
      | Market cap | 1250 | $1.3B |
      | Price | 5.386 | $5.39 |
      | Week, Month, Quarter, YTD, vs 52W high, vs 200D MA | -29.612 | -29.61% |
      | RSI (14) | 61.04 | 61.0 |
      | Rel vol | 1.2345 | 1.235 |
      | Avg vol | 842.3 | 842K |

  Scenario: Scores
    Then the composite and its five components are shown, each expandable to its components

  Scenario: Runway panel
    Then it shows cash, operating burn, trial burn, burn source, balance-sheet date, runway and the cap flag

  Scenario: Catalyst panel
    Then it shows the chosen date, phase, overdue flag and the list of considered studies

  Scenario: Short-selling panel
    Then it shows the latest short-volume ratio as a percentage, the trend, and the change in percentage points
    And it is labelled "short volume, not short interest"

  Scenario: Insider activity panel
    Then it shows Form 4 filings in 90 days, open-market buys and sales,
         officers and directors buying and selling, and the share of sale dollars under 10b5-1

  Scenario: Trial panel
    Then it shows total studies, active Phase 2 and Phase 3, the latest trial and its status,
         the next-milestone card, the pipeline list with phase chips, indications and upcoming completions

  Scenario: Read model only
    Given the company page is requested
    Then the data comes from one read of the company_page view plus the read-time trial derivations
    And the page does not join base tables

  Scenario: Every page field is populated or explained
    Given any company in the universe
    Then each of the 55 page fields shows a value or a reason for its absence

  Scenario Outline: Stale banners
    Given <condition>
    Then the page shows a banner "<message>"
    Examples:
      | condition | message |
      | earnings date earlier than today | Earnings date has passed |
      | catalyst date earlier than today | Catalyst date has passed |
      | balance-sheet date older than 120 days | Cash data is stale |
      | a carried-forward input | Using data from N days ago |
      | snapshot older than 3 days | Market data is stale |
      | latest short-volume date older than 5 trading days | Short-volume data is stale |
      | insider metrics older than 7 days | Insider data is stale |

  Scenario: Vintage footer
    Then each block lists its as-of date and source

  Scenario: Unknown ticker
    Then the page returns 404
```

**Acceptance criteria**
- A18.1 Tile values and formats equal the specified displays for the ABEO fixture.
- A18.2 Every stale condition shows its banner.
- A18.3 Each block shows a vintage.
- A18.4 Page renders in under 500 ms on the golden day.
- A18.5 Sign-in is required.
- A18.6 For every company in the golden universe, each of the 55 page fields has a value or a reason.
- A18.7 The page issues no query against a base table; its data comes from `company_page` and `ct_study_version` only.

**Open items**
| Item | Default |
|---|---|
| ABEO fixture values come from the report (price as of 2026-09-16) | Used as a display-format fixture only |
| The ABEO report shows market cap unrounded ($307.94M); the page rule is "$xM / $x.xB" | The page rule wins; confirm |
| Signed percentages: two decimals assumed | Confirm |
| Average volume is stored in thousands; displayed with a K suffix | Confirm |
| PDF export of the page | Later |
| Fair value tiles | Placeholders until the valuation phase |

**Test plan**
| ID | Level | Test | Data | Pass condition |
|---|---|---|---|---|
| T18.1 | Unit | Block assembler for each stale trigger | synthetic dates | Banner present or absent |
| T18.2 | Unit | Tile and field formatting | tables above | Exact strings |
| T18.3 | Integration | Page render for a fixture company | golden day | All blocks present |
| T18.4 | Integration | 404 and sign-in | none | As specified |
| T18.5 | Manual | Side-by-side with the report layout | F9 | Product owner signs off |
| T18.6 | Integration | Field coverage: for each company, every one of the 55 fields has a value or a reason | golden day and fixtures | 55 of 55 for every company |
| T18.7 | Integration | Count queries per page render and which relations they touch | golden day | Only `company_page` and `ct_study_version` |

---

## Step 19: Deployment and operations

**Depends on:** Step 0. **Size:** S.

```gherkin
Feature: Production operations

  Scenario: Deployment
    When the deploy workflow runs
    Then the application, database, reverse proxy and timers are running on the server over HTTPS

  Scenario: Scheduled stages
    Then the market and filings stages are started by timers at the configured times

  Scenario: Backups
    Then a nightly database dump is written to object storage and kept for 30 days

  Scenario: Restore
    Given a backup
    When the restore procedure is followed on a clean host
    Then the application starts with the same data

  Scenario: Access protection
    Then the site is reachable only by signed-in users from allowed networks

  Scenario: Monitoring
    Then errors are reported to the error tracker and disk usage above 80 percent raises an alert
```

**Acceptance criteria**
- A19.1 A deploy from CI succeeds on a clean server.
- A19.2 A restore drill succeeds and is documented.
- A19.3 Timers run both stages in staging for three consecutive days.
- A19.4 Secrets live outside the repository.

**Open items**
| Item | Default |
|---|---|
| Hosting provider, domain and bucket | A single low-cost VPS, to be chosen by the engineer |
| Retention of backups | 30 days |

**Test plan**
| ID | Level | Test | Pass condition |
|---|---|---|---|
| T19.1 | E2E | Deploy to staging | Healthy |
| T19.2 | Manual | Restore drill on a clean host | Data matches |
| T19.3 | E2E | Three days of timers in staging | Runs recorded |
| T19.4 | Manual | Access check from a disallowed network | Blocked |

---

## Step 20: Parallel run and acceptance

**Depends on:** all previous steps. **Size:** M.

```gherkin
Feature: Acceptance against the current system

  Scenario: Daily comparison
    Given both systems produce scores for the same day
    When the daily diff report runs
    Then it lists each company whose composite or any component differs by more than 0.05

  Scenario: Page fields
    Given both systems show the 55 company-page fields for the same day
    Then counts match exactly and ratios match within display rounding
    And every difference is classified

  Scenario: Explained differences
    Then each difference is classified as one of: carried-forward input, known quirk fix,
         source data timing, or unexplained

  Scenario: Pass condition
    Given 14 consecutive days
    Then at least 99 percent of companies are within tolerance on at least 12 of the days
    And no difference is unexplained for more than 2 days

  Scenario: Sign-off
    Then the product owner and the engineer sign the checklist
```

**Acceptance criteria**
- A20.1 Diff report runs daily for 14 days.
- A20.2 The pass condition holds.
- A20.3 Every remaining difference is classified.
- A20.4 The checklist is signed.

**Open items**
| Item | Default |
|---|---|
| How daily current-system outputs are obtained (the analyst has no direct access) | A daily export from the person with access |
| Tolerance | 0.05 on scores |
| What counts as an explained difference | The four classes above |

**Test plan**
| ID | Level | Test | Pass condition |
|---|---|---|---|
| T20.1 | E2E | Diff report on two known days | Correct classification |
| T20.2 | Manual | Fourteen-day review | Pass condition met |
| T20.3 | Manual | Sign-off checklist | Signed |
| T20.4 | Integration | Compare all 55 fields for the same day | Counts exact, ratios within rounding |

---

## Consolidated open items

| # | Open item | Blocks | Default | Owner |
|---|---|---|---|---|
| O1 | Real `eps_this` values | Step 3 (432 rows) | Expected-fail test, column always ingested | Product owner |
| O2 | Raw Finviz CSV sample | Step 10 | Engineer downloads one | Engineer |
| O3 | Recorded SEC responses for six sample cases | Steps 11, 12 | Engineer records from production host | Engineer |
| O4 | Trial-burn details: active statuses, multi-phase, R&D fallback, annual vs latest operating cash flow | Step 12 | Defaults in Step 12 | Product owner |
| O5 | Remaining catalyst rule (8 of 79 sampled dates) and the "active" definition conflict | Step 14 | Rule v0 | Product owner |
| O6 | Composite floor default (5 gives about 139 names, about 5.6 gives about 80) | Step 8 | 5, adjustable | Product owner |
| O7 | Null handling and near-the-line definition for the screener | Step 8 | As specified, to confirm | Product owner |
| O8 | Finviz terms on stored history and on AI processing; duplicate 52-week headers | Step 10, Phase 4 | Storage proceeds; percent distance | Product owner |
| O9 | Sponsor-matching metric and threshold | Step 13 | 0.6 token ratio with overrides | Engineer |
| O10 | Alert channel and mail provider | Step 17 | Email | Product owner |
| O11 | Hosting, domain, bucket | Step 19 | Single VPS | Engineer |
| O12 | Daily outputs from the current system for the parallel run | Step 20 | Export from its owner | Product owner |
| O13 | Whether the first production version keeps all parity quirks | Step 4, 20 | Keep, with carry-forward on | Product owner |
| O14 | Fixture files and licensing (kept out of the repository) | Step 6 | Private storage | Engineer |
| O15 | When FINRA publishes each day's file | Step 11A | Fetch in Stage B; retry next run | Engineer |
| O16 | Ticker format differences between FINRA and Finviz | Step 11A | Normalise both sides | Engineer |
| O17 | Form 4 rules: amendments, derivative transactions, issuer filter, 10b5-1 checkbox history | Step 11B | Amendment replaces original; non-derivative only; issuer filings; earlier filings count as not 10b5-1 | Product owner and engineer |
| O18 | Form 4 volume and first-run backfill time | Step 11B, 16 | Measure on first run | Engineer |
| O19 | Maximum carry-forward age for short-volume and insider metrics | Steps 11A, 11B, 15 | 5 trading days and 7 days | Product owner |
| O20 | Recorded FINRA files and Form 4 documents (F11, F12) | Steps 11A, 11B | Engineer records them | Engineer |
| O21 | Signed-percent decimals and the average-volume suffix on the page | Step 18 | Two decimals; K suffix | Product owner |
| O22 | Market cap rounding ($308M) versus the ABEO report ($307.94M) | Step 18 | Page rule wins | Product owner |
| O23 | Display of a capped runway | Steps 12, 18 | "360+ months" | Product owner |
| O24 | "Phase 2/3" in next-milestone ordering; indications de-duplication parity | Step 13A | Phase 3; no de-duplication | Product owner |
| O25 | `company_page` as a view or a materialised table | Steps 5, 9, 18 | View first; materialise if slow | Engineer |
| O26 | Carry-forward through the metric views (age from the latest source row) instead of stored carried flags | Steps 11A, 11B, 15 | View semantics as written | Engineer |

## Step dependency summary

```
0 ─┬─ 5 ─┬─ 6 ─┐
   │     │     ├─ 7 ─ 8 ─ 9
   │     ├─ 10 │
   │     ├─ 11 ─┬─ 12 ─┐
   │     │      └─ 11B ┤
   │     ├─ 11A ───────┤
   │     ├─ 13 ─ 13A ─ 14 ─┼─ 15 ─ 16 ─ 17
   │     │           │
1,2,3 ─ 4 ───────────┘ (4 feeds 7)
   └─ 19
7 ─ 18
all ─ 20
```

Steps 1 to 4 (scoring core) can start on day one in parallel with Steps 0 and 5. After Step 5, Steps 10, 11, 11A and 13 are independent of each other. Step 11B needs Step 11 (the SEC client and the shared rate limiter).
Step 13A needs only stored studies from Step 13 and feeds the trial panel in Step 18.

---

## Appendix B: traceability of the 55 company-page fields

Each field is collected or derived for every company in the universe (G5) and is read through the `company_page` view (G6). The short-volume and insider fields are views over the raw tables (v5). Test IDs ending in `.x` refer to the step's own tests; T18.6 checks that every field has a value or a reason.

| # | Page field | Collected or derived in step | Tests |
|---|---|---|---|
| 1 | Ticker | 10, 18 | T10.x, T18.2, T18.6 |
| 2 | Company | 10, 18 | T10.x, T18.2, T18.6 |
| 3 | Market cap | 10, 18 | T10.x, T18.2, T18.6 |
| 4 | Price chart | 18 | T18.3 |
| 5 | Cash + Cat. Score | 7, 18 | T7.x, T18.6 |
| 6 | Spring | 7, 18 | T7.x, T18.6 |
| 7 | Setup | 7, 18 | T7.x, T18.6 |
| 8 | Timing | 7, 18 | T7.x, T18.6 |
| 9 | Outlier | 7, 18 | T7.x, T18.6 |
| 10 | Recency (feeds composite, not shown) | 7, 18 | T7.x, T18.6 |
| 11 | Price | 10, 18 | T10.x, T18.2, T18.6 |
| 12 | Market cap | 10, 18 | T10.x, T18.2, T18.6 |
| 13 | vs 52W high | 10, 18 | T10.x, T18.2, T18.6 |
| 14 | vs 200D MA | 10, 18 | T10.x, T18.2, T18.6 |
| 15 | Latest short ratio | 11A, 18 | T11A.x, T18.6 |
| 16 | Trend | 11A, 18 | T11A.x, T18.6 |
| 17 | Δ vs 30d baseline | 11A, 18 | T11A.x, T18.6 |
| 18 | RSI (14) | 10, 18 | T10.x, T18.2, T18.6 |
| 19 | Avg vol | 10, 18 | T10.x, T18.2, T18.6 |
| 20 | Rel vol | 10, 18 | T10.x, T18.2, T18.6 |
| 21 | Week | 10, 18 | T10.x, T18.2, T18.6 |
| 22 | Month | 10, 18 | T10.x, T18.2, T18.6 |
| 23 | Quarter | 10, 18 | T10.x, T18.2, T18.6 |
| 24 | YTD | 10, 18 | T10.x, T18.2, T18.6 |
| 25 | Runway (quarters) | 12, 18 | T12.x, T18.6 |
| 26 | Runway months | 12, 18 | T12.x, T18.6 |
| 27 | Runway rating | 12, 18 | T12.x, T18.6 |
| 28 | Cash/share | 10, 18 | T10.x, T18.2, T18.6 |
| 29 | Quick ratio | 10, 18 | T10.x, T18.2, T18.6 |
| 30 | Debt/equity | 10, 18 | T10.x, T18.2, T18.6 |
| 31 | Inst. transactions | 10, 18 | T10.x, T18.2, T18.6 |
| 32 | Short float | 10, 18 | T10.x, T18.2, T18.6 |
| 33 | Insider own | 10, 18 | T10.x, T18.2, T18.6 |
| 34 | Filings (90d) | 11B, 18 | T11B.x, T18.6 |
| 35 | Open-market BUYS | 11B, 18 | T11B.x, T18.6 |
| 36 | Open-market SALES | 11B, 18 | T11B.x, T18.6 |
| 37 | Officers/dirs BUYING | 11B, 18 | T11B.x, T18.6 |
| 38 | Officers/dirs SELLING | 11B, 18 | T11B.x, T18.6 |
| 39 | % under 10b5-1 | 11B, 18 | T11B.x, T18.6 |
| 40 | Gross margin | 10, 18 | T10.x, T18.2, T18.6 |
| 41 | Op margin | 10, 18 | T10.x, T18.2, T18.6 |
| 42 | Industry | 10, 18 | T10.x, T18.2, T18.6 |
| 43 | MCap bucket | 7, 18 | T7.x, T18.6 |
| 44 | Peer group | 7, 18 | T7.x, T18.6 |
| 45 | Peer count | 7, 18 | T7.x, T18.6 |
| 46 | Earnings date | 10, 18 | T10.x, T18.2, T18.6 |
| 47 | Total studies | 13, 14, 18 | T13.x, T18.6 |
| 48 | Active Phase 3 | 13, 14, 18 | T13.x, T18.6 |
| 49 | Active Phase 2 | 13, 14, 18 | T13.x, T18.6 |
| 50 | Latest trial | 13, 14, 18 | T13.x, T18.6 |
| 51 | Latest status | 13, 14, 18 | T13.x, T18.6 |
| 52 | Next Milestone card | 13, 13A, 18 | T13A.x, T18.6 |
| 53 | Trial Pipeline total / phase chips | 13, 13A, 18 | T13A.x, T18.6 |
| 54 | Indications | 13, 13A, 18 | T13A.x, T18.6 |
| 55 | Upcoming completions list | 13, 13A, 18 | T13A.x, T18.6 |
