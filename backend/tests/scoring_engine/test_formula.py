"""T4.2 to T4.5 (unit): formula configuration, validation, quirk switches and immutability."""

from __future__ import annotations

import ast
import dataclasses
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import scoring_engine
from scoring_engine import (
    PARITY,
    CashHealthConfig,
    CompanyInputs,
    CompositeConfig,
    ConfigError,
    FormulaConfig,
    FormulaImmutableError,
    FormulaRegistry,
    FormulaVersion,
    MilestoneConfig,
    MilestoneInput,
    Quirks,
    RunwayAdjustConfig,
    TopologyConfig,
    effective_runway_months,
    milestone,
    score_universe,
)

PACKAGE = Path(scoring_engine.__file__).parent
NOW = datetime(2026, 10, 12, 20, 30, tzinfo=UTC)
TODAY = date(2026, 10, 12)


def version(**overrides: Any) -> FormulaVersion:
    """The parity version with some config sections replaced (a new, unpublished version)."""
    config = dataclasses.replace(PARITY.config, **overrides)
    return FormulaVersion(name="test", version=1, config=config)


def universe() -> list[CompanyInputs]:
    """Twelve companies in one peer group, one of them with a catalyst and earnings."""
    companies = []
    for i in range(12):
        companies.append(
            CompanyInputs(
                company_id=f"C{i}",
                industry="Biotechnology",
                mcap=60.0,
                peer_metrics={
                    "perf_ytd": float(i),
                    "perf_m": float(i * 2),
                    "perf_w": float(i % 5),
                    "rsi": 40.0 + i,
                    "inst_own": 20.0 + i * 3,
                    "short_float": 5.0 + (i % 4),
                    "vol_m": 1.0 + i,
                },
                quick_ratio=2.0 + i / 4,
                cash_per_share=1.0,
                price=2.0,
                debt_to_equity=0.2,
                runway_months=10.0,
                runway_balance_sheet_date="2026-04-12",
                clinical_date_text="2027-01-15",
                clinical_phase="phase_3",
                active_phase_3=1,
                latest_trial_status="RECRUITING",
                earnings_date_text="9/20/2026 8:30:00 AM",
                eps_next=25.0,
                rel_vol=1.0 + i / 10,
            )
        )
    return companies


# ---------------------------------------------------------------- parity and configuration


def test_parity_version_uses_the_default_constants() -> None:
    assert PARITY.config == FormulaConfig()
    assert PARITY.quirks == Quirks()
    assert PARITY.config.composite.cash_weight == 0.30


def test_parity_quirks_match_the_architecture() -> None:
    quirks = PARITY.quirks
    assert (quirks.earnings_past_is_imminent, quirks.runway_as_filed) == (True, True)
    assert (quirks.ocf_ytd_naive_sum, quirks.trial_burn_on_profitable) == (True, True)
    assert quirks.carry_forward is False


def test_scoring_runs_under_the_parity_version() -> None:
    scores = score_universe(universe(), NOW, PARITY)
    assert len(scores) == 12
    assert all(0 <= s.composite.score <= 10 for s in scores.values())


def test_changing_the_cash_weight_moves_composite_but_not_the_components() -> None:
    base = score_universe(universe(), NOW, PARITY)
    changed = score_universe(universe(), NOW, version(composite=CompositeConfig(cash_weight=0.25)))
    for company_id, before in base.items():
        after = changed[company_id]
        assert after.cash_health.score == before.cash_health.score
        assert after.milestone.score == before.milestone.score
        assert after.topology == before.topology
        assert after.composite.score == pytest.approx(
            before.composite.score - 0.05 * before.cash_health.score
        )


@pytest.mark.parametrize(
    ("overrides", "moved"),
    [
        ({"cash_health": CashHealthConfig(factor_near=0.5)}, {"cash_health", "composite"}),
        ({"milestone": MilestoneConfig(eps_next_weight=3.0)}, {"milestone", "composite"}),
        ({"topology": TopologyConfig(outlier_scale=1.0)}, {"topology", "composite"}),
        ({"composite": CompositeConfig(outlier_weight=0.0)}, {"composite"}),
    ],
)
def test_each_constant_moves_only_the_expected_outputs(
    overrides: dict[str, Any], moved: set[str]
) -> None:
    base = score_universe(universe(), NOW, PARITY)["C3"]
    other = score_universe(universe(), NOW, version(**overrides))["C3"]
    changed = {
        name
        for name in ("topology", "cash_health", "milestone", "composite")
        if getattr(other, name) != getattr(base, name)
    }
    assert changed == moved


CONFIG_CLASSES: dict[str, list[type]] = {
    "topology.py": [TopologyConfig],
    "cash_health.py": [CashHealthConfig],
    "milestone.py": [MilestoneConfig],
    "composite.py": [CompositeConfig],
    "effective_runway.py": [RunwayAdjustConfig],
}


@pytest.mark.parametrize("module", sorted(CONFIG_CLASSES))
def test_every_config_field_is_read_by_the_scoring_code(module: str) -> None:
    """A constant that nothing reads is a constant that configuration cannot change."""
    tree = ast.parse((PACKAGE / module).read_text(encoding="utf-8"))
    read = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for cls in CONFIG_CLASSES[module]:
        unread = [f.name for f in dataclasses.fields(cls) if f.name not in read]
        assert not unread, f"{cls.__name__} fields never read: {unread}"


# ------------------------------------------------------------------- T4.4 strict validation


def test_round_trip_through_plain_data() -> None:
    plain = PARITY.to_plain()
    assert json.loads(json.dumps(plain)) == plain
    assert FormulaVersion.from_plain(plain) == PARITY


def test_partial_configuration_keeps_defaults() -> None:
    loaded = FormulaVersion.from_plain(
        {"name": "x", "version": 2, "config": {"composite": {"cash_weight": 0.25}}}
    )
    assert loaded.config.composite.cash_weight == 0.25
    assert loaded.config.composite.milestone_weight == 0.35
    assert loaded.quirks == Quirks()


def test_integers_are_accepted_for_numbers() -> None:
    loaded = FormulaVersion.from_plain(
        {"name": "x", "version": 1, "config": {"composite": {"cash_weight": 1}}}
    )
    assert loaded.config.composite.cash_weight == 1.0


@pytest.mark.parametrize(
    ("bad", "path"),
    [
        ({"config": {"composite": {"cash_wieght": 0.3}}}, "config.composite.cash_wieght"),
        ({"config": {"composit": {}}}, "config.composit"),
        ({"quirks": {"made_up_switch": True}}, "quirks.made_up_switch"),
        ({"surprise": 1}, "surprise"),
        ({"config": {"composite": {"cash_weight": "0.3"}}}, "config.composite.cash_weight"),
        ({"config": {"composite": {"cash_weight": True}}}, "config.composite.cash_weight"),
        ({"config": {"composite": {"cash_weight": float("nan")}}}, "config.composite.cash_weight"),
        ({"config": {"composite": {"cash_weight": None}}}, "config.composite.cash_weight"),
        ({"quirks": {"carry_forward": "yes"}}, "quirks.carry_forward"),
        ({"config": {"topology": {"min_peers": 10.5}}}, "config.topology.min_peers"),
        ({"config": {"topology": {"min_peers": True}}}, "config.topology.min_peers"),
        ({"config": {"cash_health": {"quick_ratio_buckets": [[5.0]]}}}, "quick_ratio_buckets[0]"),
        (
            {"config": {"cash_health": {"quick_ratio_buckets": [[5.0, "x"]]}}},
            "quick_ratio_buckets[0][1]",
        ),
        ({"config": {"cash_health": {"quick_ratio_buckets": 5}}}, "quick_ratio_buckets"),
        ({"config": {"topology": {"dimensions": ["perf_ytd", 3]}}}, "dimensions[1]"),
        ({"config": {"topology": {"bucket_cutoffs": [[1, 2]]}}}, "bucket_cutoffs[0][0]"),
        ({"config": []}, "config"),
    ],
)
def test_invalid_configuration_names_the_key(bad: dict[str, Any], path: str) -> None:
    with pytest.raises(ConfigError, match=path.replace("[", r"\[").replace("]", r"\]")):
        FormulaVersion.from_plain({"name": "x", "version": 1, **bad})


@pytest.mark.parametrize(
    ("changes", "match"),
    [({"name": ""}, "name"), ({"version": "1"}, "version"), ({"version": True}, "version"),
     ({"notes": 3}, "notes")],
)  # fmt: skip
def test_invalid_identity_fields(changes: dict[str, Any], match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        FormulaVersion.from_plain({"name": "x", "version": 1, **changes})


def test_bucket_tables_survive_a_round_trip_as_tuples() -> None:
    loaded = FormulaVersion.from_plain(PARITY.to_plain())
    assert isinstance(loaded.config.cash_health.quick_ratio_buckets, tuple)
    assert loaded.config.topology.bucket_cutoffs[0] == ("Nano", 25.0)


# --------------------------------------------------------------------------- T4.5 immutability


def test_content_hash_ignores_name_and_notes_but_not_values() -> None:
    renamed = dataclasses.replace(PARITY, name="other", notes="n")
    assert renamed.content_hash() == PARITY.content_hash()
    assert (
        version(composite=CompositeConfig(cash_weight=0.25)).content_hash() != PARITY.content_hash()
    )
    assert PARITY.with_quirks(carry_forward=True).content_hash() != PARITY.content_hash()


def test_a_published_version_cannot_be_republished_with_different_content() -> None:
    registry = FormulaRegistry()
    registry.publish(PARITY)
    registry.publish(PARITY)  # identical content is harmless
    changed = dataclasses.replace(
        PARITY, config=FormulaConfig(composite=CompositeConfig(cash_weight=0.25))
    )
    with pytest.raises(FormulaImmutableError, match="new version"):
        registry.publish(changed)


def test_a_used_version_cannot_be_edited() -> None:
    registry = FormulaRegistry()
    registry.publish(PARITY)
    changed = dataclasses.replace(
        PARITY, config=FormulaConfig(composite=CompositeConfig(cash_weight=0.25))
    )
    registry.edit(changed)  # not used yet, so it can still change
    registry.mark_used("parity", 1)
    with pytest.raises(FormulaImmutableError, match="used by a score run"):
        registry.edit(PARITY)
    assert registry.get("parity", 1) == changed


def test_a_new_version_can_be_published_beside_a_used_one() -> None:
    registry = FormulaRegistry()
    registry.publish(PARITY)
    registry.mark_used("parity", 1)
    registry.publish(
        dataclasses.replace(
            PARITY, version=2, config=FormulaConfig(composite=CompositeConfig(cash_weight=0.25))
        )
    )
    assert registry.get("parity", 2).config.composite.cash_weight == 0.25


def test_marking_or_editing_an_unknown_version_fails() -> None:
    registry = FormulaRegistry()
    with pytest.raises(KeyError):
        registry.mark_used("nope", 1)
    with pytest.raises(KeyError):
        registry.edit(PARITY)


# ------------------------------------------------------------------------ T4.3 quirk switches


def test_earnings_past_is_imminent_both_ways() -> None:
    data = MilestoneInput(
        clinical_date=TODAY + timedelta(days=300),
        earnings_date=TODAY - timedelta(days=40),
    )
    midnight = datetime(2026, 10, 12, tzinfo=UTC)
    parity = milestone(data, midnight, earnings_past_is_imminent=True)
    fixed = milestone(data, midnight, earnings_past_is_imminent=False)
    assert next(t for t in parity.terms if t.name == "earnings").score == 10.0
    assert "earnings" not in {t.name for t in fixed.terms}
    assert fixed.score != parity.score


def test_earnings_today_still_counts_when_the_quirk_is_off() -> None:
    midnight = datetime(2026, 10, 12, tzinfo=UTC)
    data = MilestoneInput(active_phase_2=1, earnings_date=TODAY)
    result = milestone(data, midnight, earnings_past_is_imminent=False)
    assert "earnings" in {t.name for t in result.terms}


def test_earnings_quirk_flows_through_the_universe_score() -> None:
    parity = score_universe(universe(), NOW, PARITY)["C0"].milestone
    fixed = score_universe(universe(), NOW, PARITY.with_quirks(earnings_past_is_imminent=False))[
        "C0"
    ].milestone
    assert "earnings" in {t.name for t in parity.terms}
    assert "earnings" not in {t.name for t in fixed.terms}


def test_runway_as_filed_both_ways() -> None:
    as_filed = effective_runway_months(10.0, date(2026, 4, 12), TODAY, runway_as_filed=True)
    adjusted = effective_runway_months(10.0, date(2026, 4, 12), TODAY, runway_as_filed=False)
    assert as_filed == 10.0
    assert adjusted == pytest.approx(10.0 - 183 / 30.44)


def test_adjusted_runway_is_floored_at_zero() -> None:
    assert effective_runway_months(2.0, date(2025, 1, 1), TODAY, runway_as_filed=False) == 0.0


def test_runway_cannot_be_adjusted_without_a_balance_sheet_date() -> None:
    assert effective_runway_months(10.0, None, TODAY, runway_as_filed=False) == 10.0
    assert effective_runway_months(None, date(2026, 1, 1), TODAY, runway_as_filed=False) is None
    assert effective_runway_months(float("nan"), None, TODAY) is None


def test_a_future_balance_sheet_date_does_not_add_runway() -> None:
    result = effective_runway_months(10.0, TODAY + timedelta(days=30), TODAY, runway_as_filed=False)
    assert result == 10.0


def test_runway_quirk_flows_through_the_ceiling_and_the_boost() -> None:
    parity = score_universe(universe(), NOW, PARITY)["C3"]
    fixed = score_universe(universe(), NOW, PARITY.with_quirks(runway_as_filed=False))["C3"]
    assert parity.runway_used == 10.0
    assert fixed.runway_used == pytest.approx(10.0 - 183 / 30.44)
    assert fixed.runway_used != parity.runway_used
    assert parity.cash_health.runway_cap == 5.0  # 10 months as filed
    assert fixed.cash_health.runway_cap == 3.0  # about 4 months remaining


def test_with_quirks_rejects_unknown_switches() -> None:
    with pytest.raises(ConfigError, match="quirks.nonsense"):
        PARITY.with_quirks(nonsense=True)


def test_score_universe_needs_an_aware_now() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        score_universe(universe(), datetime(2026, 10, 12), PARITY)  # noqa: DTZ001
