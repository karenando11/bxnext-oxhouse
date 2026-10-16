"""Insider metrics as a function of the market date, plus a view for today.

The window is 90 days and includes day 90. Only successfully parsed Form 4 and 4/A filings count,
and a filing is dropped when a successfully parsed amendment replaces it. Buys are transactions
with code P and sales code S. The officer and director counts are distinct reporting owners who
are officers or directors on filings that contain a purchase (or a sale). ``pct_10b5_1_dollars``
is the share of open-market sale dollars in filings flagged 10b5-1, NULL without sales.

A company with no insider filings has zero counts and a NULL percentage, which is a result. Counts
are NULL only when the company's submissions feed has never been fetched successfully.
``age_days`` is the age of the last successful submissions call; stale after 7 days.
"""

from django.db import migrations

FUNCTION = """
CREATE FUNCTION insider_metric_at(as_of date)
RETURNS TABLE (
    company_id bigint,
    as_of_date date,
    window_days integer,
    filings_count integer,
    open_market_buys_count integer,
    open_market_sales_count integer,
    officers_directors_buying_count integer,
    officers_directors_selling_count integer,
    pct_10b5_1_dollars double precision,
    status text,
    reason_codes text[],
    age_days integer,
    is_stale boolean
)
LANGUAGE sql STABLE AS $$
WITH feed AS (
    SELECT c.id AS company_id,
           max(sc.completed_at) AS last_ok
    FROM companies_company c
    LEFT JOIN platform_sourcecall sc
           ON sc.feed = 'sec_submissions' AND sc.scope_key = c.cik AND sc.status = 'ok'
              AND sc.completed_at IS NOT NULL
    WHERE c.status = 'active'
    GROUP BY c.id
),
live AS (
    SELECT f.id, f.company_id, f.is_10b5_1
    FROM sec_form4_filing_effective f
    WHERE f.filed_at <= as_of AND f.filed_at >= as_of - 90
),
tx AS (
    SELECT l.company_id, l.id AS filing_id, l.is_10b5_1, t.transaction_code,
           t.reporting_owner_cik, t.is_officer, t.is_director, t.amount_usd
    FROM live l
    JOIN sec_secform4transaction t ON t.filing_id = l.id
),
per_company AS (
    SELECT l.company_id,
           count(DISTINCT l.id)::integer AS filings,
           count(*) FILTER (WHERE x.transaction_code = 'P')::integer AS buys,
           count(*) FILTER (WHERE x.transaction_code = 'S')::integer AS sales
    FROM live l
    LEFT JOIN tx x ON x.filing_id = l.id
    GROUP BY l.company_id
),
owners AS (
    SELECT x.company_id,
           count(DISTINCT x.reporting_owner_cik) FILTER (
               WHERE (x.is_officer OR x.is_director)
                 AND x.filing_id IN (SELECT filing_id FROM tx WHERE transaction_code = 'P')
           )::integer AS buying,
           count(DISTINCT x.reporting_owner_cik) FILTER (
               WHERE (x.is_officer OR x.is_director)
                 AND x.filing_id IN (SELECT filing_id FROM tx WHERE transaction_code = 'S')
           )::integer AS selling,
           sum(x.amount_usd) FILTER (WHERE x.transaction_code = 'S') AS sale_dollars,
           sum(x.amount_usd) FILTER (WHERE x.transaction_code = 'S' AND x.is_10b5_1)
               AS planned_sale_dollars
    FROM tx x
    GROUP BY x.company_id
)
SELECT f.company_id,
       as_of,
       90,
       CASE WHEN f.last_ok IS NULL THEN NULL ELSE coalesce(p.filings, 0) END,
       CASE WHEN f.last_ok IS NULL THEN NULL ELSE coalesce(p.buys, 0) END,
       CASE WHEN f.last_ok IS NULL THEN NULL ELSE coalesce(p.sales, 0) END,
       CASE WHEN f.last_ok IS NULL THEN NULL ELSE coalesce(o.buying, 0) END,
       CASE WHEN f.last_ok IS NULL THEN NULL ELSE coalesce(o.selling, 0) END,
       CASE WHEN o.sale_dollars > 0
            THEN (100 * coalesce(o.planned_sale_dollars, 0) / o.sale_dollars)::double precision
       END,
       CASE WHEN f.last_ok IS NULL THEN 'unavailable' ELSE 'ok' END,
       CASE WHEN f.last_ok IS NULL THEN ARRAY['fetch_error']::text[] ELSE ARRAY[]::text[] END,
       (as_of - f.last_ok::date)::integer,
       coalesce((as_of - f.last_ok::date) > 7, true)
FROM feed f
LEFT JOIN per_company p ON p.company_id = f.company_id
LEFT JOIN owners o ON o.company_id = f.company_id
$$;
"""

# A filing that failed to parse never counts; an original is dropped when a parsed amendment
# replaces it.
EFFECTIVE_FILINGS = """
CREATE VIEW sec_form4_filing_effective AS
SELECT f.*
FROM sec_secform4filing f
WHERE f.parse_status = 'ok'
  AND NOT EXISTS (
      SELECT 1 FROM sec_secform4filing a
      WHERE a.amends_accession = f.accession AND a.parse_status = 'ok'
  );
"""

VIEW = """
CREATE VIEW insider_metric_daily AS
SELECT * FROM insider_metric_at((now() AT TIME ZONE 'America/New_York')::date);
"""


class Migration(migrations.Migration):
    dependencies = [("sec", "0001_initial")]

    operations = [
        migrations.RunSQL(EFFECTIVE_FILINGS, "DROP VIEW IF EXISTS sec_form4_filing_effective"),
        migrations.RunSQL(FUNCTION, "DROP FUNCTION IF EXISTS insider_metric_at(date)"),
        migrations.RunSQL(VIEW, "DROP VIEW IF EXISTS insider_metric_daily"),
    ]
