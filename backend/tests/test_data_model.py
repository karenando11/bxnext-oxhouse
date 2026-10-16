"""T5.1 to T5.8 (integration): tables, keys, seeds, reason codes and formula immutability."""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from django.apps import apps as django_apps
from django.core.management import call_command
from django.db import DatabaseError, IntegrityError, connection, models, transaction

from apps.platform.models import Setting
from apps.platform.reasons import REASON_CODES
from apps.screener.models import ScreenDefinition
from scoring_engine import PARITY
from tests import factories as f

PHASE_1_TABLES = {
    "platform_pipelinerun",
    "platform_sourcecall",
    "platform_dataqualitycheck",
    "platform_setting",
    "companies_company",
    "companies_tickerhistory",
    "companies_companysponsoralias",
    "marketdata_finvizsnapshot",
    "marketdata_shortvolumedaily",
    "sec_secfiling",
    "sec_secfact",
    "sec_runwaycalc",
    "sec_secform4filing",
    "sec_secform4transaction",
    "trials_ctstudyversion",
    "trials_catalystsignal",
    "scoring_formulaversion",
    "scoring_scorerun",
    "scoring_scoreresult",
    "screener_screendefinition",
    "screener_screenrun",
    "screener_screenmember",
    "screener_funnelevent",
}
VIEWS = {"short_volume_metric", "insider_metric_daily", "company_page"}
REMOVED_TABLES = {
    "platform_reasoncode",
    "platform_pipelinestep",
    "sec_runwaytrialburnline",
    "trials_catalystcandidate",
    "scoring_peergroupstat",
    "marketdata_shortvolumemetric",
    "sec_insidermetricdaily",
}


def _relations(kind: str) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = %s",
            [kind],
        )
        return {row[0] for row in cursor.fetchall()}


@pytest.mark.django_db
def test_the_23_phase_1_tables_exist() -> None:
    tables = _relations("BASE TABLE")
    assert PHASE_1_TABLES <= tables
    assert len(PHASE_1_TABLES) == 23


@pytest.mark.django_db
def test_views_exist_and_removed_tables_do_not() -> None:
    assert VIEWS <= _relations("VIEW")
    assert not (REMOVED_TABLES & _relations("BASE TABLE"))


def test_every_model_belongs_to_the_documented_set() -> None:
    managed = {
        m._meta.db_table
        for m in django_apps.get_models()
        if m._meta.app_label
        in {"platform", "companies", "marketdata", "sec", "trials", "scoring", "screener", "web"}
        and m._meta.managed
    }
    assert managed == PHASE_1_TABLES


# -------------------------------------------------------------------- T5.6 factories


FACTORIES = [
    f.pipeline_run, f.source_call, f.data_quality_check, f.setting, f.company, f.ticker,
    f.sponsor_alias, f.snapshot, f.sec_filing, f.sec_fact, f.runway_calc, f.form4_filing,
    f.form4_transaction, f.ct_study, f.catalyst_signal, f.formula_version, f.score_run,
    f.score_result, f.screen_definition, f.screen_run, f.screen_member, f.funnel_event,
]  # fmt: skip


@pytest.mark.django_db
@pytest.mark.parametrize("factory", FACTORIES, ids=lambda fn: fn.__name__)
def test_factories_create_valid_objects(factory: Any) -> None:
    obj = factory()
    obj.full_clean(validate_unique=False, validate_constraints=False, exclude=_json_fields(obj))
    assert obj.pk is not None


def _json_fields(obj: models.Model) -> list[str]:
    return [fld.name for fld in obj._meta.fields if isinstance(fld, models.JSONField)]


@pytest.mark.django_db
def test_short_volume_factory_creates_a_valid_object() -> None:
    row = f.short_volume(f.company(), f.DAY, 600, 1000)
    assert row.pk is not None


# --------------------------------------------------------------------- T5.2 natural keys


def _duplicate_is_rejected(create: Any) -> None:
    create()
    with pytest.raises(IntegrityError), transaction.atomic():
        create()


@pytest.mark.django_db
def test_finviz_snapshot_is_unique_per_company_and_day() -> None:
    co = f.company()
    _duplicate_is_rejected(lambda: f.snapshot(co))


@pytest.mark.django_db
def test_runway_calc_is_unique_per_company_and_day() -> None:
    co = f.company()
    _duplicate_is_rejected(lambda: f.runway_calc(co))


@pytest.mark.django_db
def test_catalyst_signal_is_unique_per_company_and_day() -> None:
    co = f.company()
    _duplicate_is_rejected(lambda: f.catalyst_signal(co))


@pytest.mark.django_db
def test_sec_filing_accession_is_unique() -> None:
    co = f.company()
    _duplicate_is_rejected(lambda: f.sec_filing(co, accession="0000000000-26-000001"))


@pytest.mark.django_db
def test_ct_study_is_unique_per_nct_and_content() -> None:
    co = f.company()
    _duplicate_is_rejected(lambda: f.ct_study(co, nct_id="NCT00000001", content_hash="h"))


@pytest.mark.django_db
def test_score_result_is_unique_per_run_and_company() -> None:
    run, co = f.score_run(), f.company()
    _duplicate_is_rejected(lambda: f.score_result(run, co))


@pytest.mark.django_db
def test_short_volume_is_unique_per_ticker_day_and_market() -> None:
    co = f.company()
    _duplicate_is_rejected(lambda: f.short_volume(co, f.DAY, 1, 2, ticker_raw="ABC"))


@pytest.mark.django_db
def test_form4_accession_is_unique() -> None:
    co = f.company()
    _duplicate_is_rejected(lambda: f.form4_filing(co, accession="0000000001-26-000001"))


@pytest.mark.django_db
def test_sec_fact_natural_key_treats_missing_period_start_as_equal() -> None:
    co = f.company()
    _duplicate_is_rejected(lambda: f.sec_fact(co, period_start=None))


@pytest.mark.django_db
def test_company_cik_may_repeat_for_securities_of_one_issuer_and_may_be_missing() -> None:
    f.company(cik="0000012345")
    f.company(cik="0000012345")  # a share class or when-issued line of the same issuer
    f.company(cik=None)
    f.company(cik=None)


@pytest.mark.django_db
@pytest.mark.parametrize("cik", ["123", "12345678901", "abcdefghij"])
def test_company_cik_must_be_ten_digits(cik: str) -> None:
    with pytest.raises(DatabaseError), transaction.atomic():  # too long, or the check constraint
        f.company(cik=cik)


@pytest.mark.django_db
def test_form4_line_numbers_are_unique_within_a_filing() -> None:
    filing = f.form4_filing()
    _duplicate_is_rejected(lambda: f.form4_transaction(filing, line_no=1))


@pytest.mark.django_db
def test_screen_member_is_unique_per_run_and_company() -> None:
    run, co = f.screen_run(), f.company()
    _duplicate_is_rejected(lambda: f.screen_member(screen_run=run, company=co))


@pytest.mark.django_db
def test_a_passed_screen_member_cannot_list_exclusion_reasons() -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        f.screen_member(passed=True, excluded_reasons=["price_min"])
    f.screen_member(passed=False, excluded_reasons=["price_min", "avg_vol_min"])


@pytest.mark.django_db
def test_only_one_default_screen() -> None:
    assert ScreenDefinition.objects.filter(is_default=True).count() == 1  # the seeded one
    with pytest.raises(IntegrityError), transaction.atomic():
        f.screen_definition(is_default=True)


@pytest.mark.django_db
def test_formula_version_name_and_version_are_unique() -> None:
    _duplicate_is_rejected(lambda: f.formula_version(name="dupe", version=1))


@pytest.mark.django_db
def test_foreign_keys_are_enforced_by_the_database() -> None:
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO marketdata_finvizsnapshot (company_id, as_of_date, extras, "
            "company_name_raw, sector_raw, industry_raw, earnings_date_text) "
            "VALUES (999999999, '2026-10-12', '{}', '', '', '', '')"
        )
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")  # Django defers foreign key checks


# --------------------------------------------------------------------- T5.3 one open ticker


@pytest.mark.django_db
def test_a_second_open_ticker_is_rejected() -> None:
    first = f.ticker(ticker="ONE")
    with pytest.raises(IntegrityError), transaction.atomic():
        f.ticker(ticker="ONE")
    first.valid_to = f.DAY
    first.save()
    f.ticker(ticker="ONE")  # allowed once the earlier row is closed


# ----------------------------------------------------------------------- T5.8 reason codes


@pytest.mark.django_db
def test_every_known_reason_code_is_accepted_and_unknown_ones_rejected() -> None:
    for code in REASON_CODES:
        f.source_call(reason_code=code)
    f.source_call(reason_code=None)
    with pytest.raises(IntegrityError), transaction.atomic():
        f.source_call(reason_code="made_up")


@pytest.mark.django_db
def test_reason_code_arrays_are_checked_too() -> None:
    f.runway_calc(reason_codes=["cash_missing", "stale_carried"])
    with pytest.raises(IntegrityError), transaction.atomic():
        f.runway_calc(reason_codes=["cash_missing", "made_up"])
    with pytest.raises(IntegrityError), transaction.atomic():
        f.catalyst_signal(reason_codes=["made_up"])


# --------------------------------------------------------------------------- T5.4 seeds


@pytest.mark.django_db
def test_seed_data_is_present_after_migration() -> None:
    settings = dict(Setting.objects.values_list("key", "value"))
    assert settings["max_age.sec_runway_days"] == 120
    assert settings["max_age.ctgov_days"] == 7
    assert settings["max_age.short_volume_trading_days"] == 5
    assert settings["max_age.insider_days"] == 7
    assert settings["ctgov.sponsor_match_threshold"] == 0.6
    assert settings["runway.trial_burn_rates_usd"]["phase_3"] == 2500000
    default = ScreenDefinition.objects.get(is_default=True)
    assert default.config["composite_floor"] == 5.0
    assert default.config["mcap_min_musd"] == 75
    from apps.scoring.models import FormulaVersion

    assert FormulaVersion.objects.filter(name="parity", version=1).count() == 1


@pytest.mark.django_db
def test_the_seeded_parity_version_matches_the_scoring_engine() -> None:
    from apps.scoring.models import FormulaVersion

    stored = FormulaVersion.objects.get(name="parity", version=1)
    plain = PARITY.to_plain()
    assert stored.config == plain["config"]
    assert stored.quirks == plain["quirks"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("module", "model"),
    [
        ("apps.platform.migrations.0002_seed_settings", Setting),
        ("apps.screener.migrations.0002_seed_default_screen", ScreenDefinition),
        ("apps.scoring.migrations.0002_seed_parity_formula", None),
    ],
)
def test_seeds_are_idempotent(module: str, model: type[models.Model] | None) -> None:
    from apps.scoring.models import FormulaVersion

    model = model or FormulaVersion
    before = model._default_manager.count()
    seed = importlib.import_module(module).seed
    seed(django_apps, None)
    seed(django_apps, None)
    assert model._default_manager.count() == before


@pytest.mark.django_db
def test_reseeding_does_not_overwrite_an_edited_setting() -> None:
    Setting.objects.filter(key="max_age.insider_days").update(value=14)
    importlib.import_module("apps.platform.migrations.0002_seed_settings").seed(django_apps, None)
    assert Setting.objects.get(key="max_age.insider_days").value == 14


# ----------------------------------------------------------------- formula immutability


@pytest.mark.django_db
def test_an_unused_formula_version_can_be_edited() -> None:
    version = f.formula_version()
    version.config = {"a": 2}
    version.save()
    version.refresh_from_db()
    assert version.config == {"a": 2}


@pytest.mark.django_db
def test_a_used_formula_version_cannot_be_changed() -> None:
    version = f.formula_version()
    f.score_run(formula_version=version)
    for field, value in (
        ("config", {"a": 2}),
        ("quirks", {"b": False}),
        ("name", "renamed"),
        ("version", 9),
    ):
        setattr(version, field, value)
        with pytest.raises(DatabaseError, match="create a new version"), transaction.atomic():
            version.save()
        version.refresh_from_db()


@pytest.mark.django_db
def test_notes_of_a_used_version_can_still_change() -> None:
    version = f.formula_version()
    f.score_run(formula_version=version)
    version.notes = "explained later"
    version.save()
    version.refresh_from_db()
    assert version.notes == "explained later"


@pytest.mark.django_db
def test_a_used_formula_version_cannot_be_deleted() -> None:
    version = f.formula_version()
    f.score_run(formula_version=version)
    with pytest.raises(DatabaseError), transaction.atomic():
        version.delete()


# -------------------------------------------------------------------- T5.5 performance


@pytest.mark.django_db
def test_screener_query_over_a_full_universe_is_fast() -> None:
    from time import perf_counter

    from apps.companies.models import Company
    from apps.marketdata.models import FinvizSnapshot
    from apps.scoring.models import ScoreResult

    run = f.score_run()
    companies = Company.objects.bulk_create(
        [
            Company(
                name=f"C{i}", first_seen_date=f.DAY, last_seen_date=f.DAY, cik=f"{i + 5000:010d}"
            )
            for i in range(1053)
        ]
    )
    FinvizSnapshot.objects.bulk_create(
        [
            FinvizSnapshot(
                company=c, as_of_date=f.DAY, price=2.0 + i % 9, mcap_musd=20.0 + i, avg_vol_k=60.0
            )
            for i, c in enumerate(companies)
        ]
    )
    ScoreResult.objects.bulk_create(
        [
            ScoreResult(
                score_run=run, company=c, mcap_bucket="Micro", peer_group_key="B|Micro",
                peer_n=20, is_sparse=False, cash_health=5, milestone=5, outlier=5,
                divergence=0, recency=0.5, composite=3 + (i % 70) / 10, inputs={}, components={},
            )
            for i, c in enumerate(companies)
        ]
    )  # fmt: skip
    with connection.cursor() as cursor:
        cursor.execute("ANALYZE")
    sql = (
        "SELECT sr.company_id FROM scoring_scoreresult sr "
        "JOIN marketdata_finvizsnapshot fs ON fs.company_id = sr.company_id AND fs.as_of_date = %s "
        "WHERE sr.score_run_id = %s AND sr.composite >= 5 AND fs.mcap_musd BETWEEN 75 AND 1000 "
        "AND fs.price >= 1 AND fs.avg_vol_k >= 50 ORDER BY sr.composite DESC"
    )
    started = perf_counter()
    with connection.cursor() as cursor:
        cursor.execute(sql, [f.DAY, run.pk])
        rows = cursor.fetchall()
    assert perf_counter() - started < 0.1
    assert rows


# ------------------------------------------------------------------- A5.4 units documented


@pytest.mark.django_db
def test_every_numeric_finviz_column_documents_its_meaning_and_unit() -> None:
    from apps.marketdata.models import FinvizSnapshot

    numeric = [
        fld
        for fld in FinvizSnapshot._meta.fields
        if isinstance(fld, models.FloatField) and fld.name not in {"id"}
    ]
    assert len(numeric) > 40
    missing = [fld.name for fld in numeric if not fld.help_text]
    assert not missing
    units = {
        "mcap_musd": "USD millions",
        "shr_out_m": "millions",
        "avg_vol_k": "thousands of shares",
        "perf_ytd": "percent",
    }
    for name, unit in units.items():
        assert unit in getattr(FinvizSnapshot._meta.get_field(name), "help_text", "")


# ---------------------------------------------------------------- T5.1 up, down, up


@pytest.mark.django_db(transaction=True)
def test_migrations_reverse_and_reapply_cleanly() -> None:
    labels = ["web", "screener", "scoring", "sec", "trials", "marketdata", "platform", "companies"]
    for label in labels:
        call_command("migrate", label, "zero", verbosity=0)
    assert not (PHASE_1_TABLES & _relations("BASE TABLE"))
    assert not (VIEWS & _relations("VIEW"))
    call_command("migrate", verbosity=0)
    assert PHASE_1_TABLES <= _relations("BASE TABLE")
    assert VIEWS <= _relations("VIEW")
    assert Setting.objects.filter(key="max_age.sec_runway_days").exists()
