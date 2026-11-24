"""T13A: trial panel values derived from stored studies (no clock, no stored output)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from apps.trials.display import (
    Study,
    drug_name,
    indications,
    latest_studies,
    next_milestone,
    phase_chips,
    quarters_out,
    trial_panel,
    upcoming_completions,
)
from tests import factories as f

TODAY = date(2026, 10, 12)


def study(nct: str, phase: str = "PHASE2", primary: date | None = None, **kw: object) -> Study:
    return Study(nct, highest_phase=phase, primary_completion=primary, **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("days", "display"),
    [(182, "2 quarters out"), (20, "1 quarter out"), (0, "Past due"), (-40, "Past due"),
     (1, "1 quarter out"), (91, "1 quarter out"), (92, "2 quarters out"), (365, "4 quarters out")],
)  # fmt: skip
def test_quarters_out(days: int, display: str) -> None:
    assert quarters_out(TODAY + timedelta(days=days), TODAY) == display


def test_quarters_out_without_a_date_is_blank() -> None:
    assert quarters_out(None, TODAY) == ""


def test_phase_3_leads_even_when_later() -> None:
    studies = [
        study("A", "PHASE2", TODAY + timedelta(days=10)),
        study("B", "PHASE3", TODAY + timedelta(days=400)),
    ]
    chosen = next_milestone(studies, TODAY)
    assert chosen is not None and chosen.nct_id == "B"


def test_without_phase_3_the_soonest_wins_and_past_dates_stay() -> None:
    studies = [
        study("A", "PHASE2", TODAY + timedelta(days=10)),
        study("B", "PHASE1", TODAY - timedelta(days=90)),
        study("C", "PHASE2", None),
    ]
    chosen = next_milestone(studies, TODAY)
    assert chosen is not None and chosen.nct_id == "B"  # overdue, still chosen
    assert quarters_out(chosen.milestone_date, TODAY) == "Past due"


def test_overdue_phase_3_still_leads() -> None:
    studies = [
        study("A", "PHASE2", TODAY + timedelta(days=10)),
        study("B", "PHASE3", TODAY - timedelta(days=5)),
    ]
    chosen = next_milestone(studies, TODAY)
    assert chosen is not None and chosen.nct_id == "B"


def test_no_dated_study_has_no_milestone() -> None:
    assert next_milestone([study("A"), study("B")], TODAY) is None


def test_phase_2_3_study_counts_as_phase_3() -> None:
    chosen = next_milestone(
        [
            study("A", "PHASE2", TODAY + timedelta(days=1)),
            study("B", "PHASE3", TODAY + timedelta(days=9)),
        ],
        TODAY,
    )
    assert chosen is not None and chosen.nct_id == "B"


def test_milestone_date_prefers_the_primary_date() -> None:
    s = Study("A", primary_completion=date(2027, 1, 1), completion=date(2028, 1, 1))
    assert s.milestone_date == date(2027, 1, 1)
    assert Study("B", completion=date(2028, 1, 1)).milestone_date == date(2028, 1, 1)


def test_drug_name_strips_the_type_prefix() -> None:
    assert (
        drug_name(Study("A", interventions=({"type": "DRUG", "name": "DRUG: Zimura"},))) == "Zimura"
    )
    assert drug_name(
        Study("A", interventions=({"type": "DRUG", "name": "Zimura (avacincaptad)"},))
    ) == ("Zimura (avacincaptad)")
    assert drug_name(Study("A", interventions=({"name": ""}, {"name": "Placebo"}))) == "Placebo"
    assert drug_name(Study("A")) == ""


def test_pipeline_total_and_one_chip_per_phase() -> None:
    studies = (
        [study(f"P1-{i}", "PHASE1") for i in range(2)]
        + [study(f"P2-{i}", "PHASE2") for i in range(3)]
        + [study(f"P3-{i}", "PHASE3") for i in range(2)]
    )
    panel = trial_panel(studies, TODAY)
    assert panel.pipeline_total == 7
    assert panel.phase_chips == ["PHASE1", "PHASE2", "PHASE3"]


def test_chips_ignore_empty_phase_and_order_early_phase_first() -> None:
    assert phase_chips([study("A", ""), study("B", "PHASE3"), study("C", "EARLY_PHASE1")]) == [
        "EARLY_PHASE1",
        "PHASE3",
    ]


def test_indications_are_not_deduplicated() -> None:
    studies = [
        study("A", conditions=("Geographic Atrophy", "AMD")),
        study("B", conditions=("Geographic Atrophy",)),
    ]
    assert indications(studies) == ["Geographic Atrophy", "AMD", "Geographic Atrophy"]


def test_upcoming_completions() -> None:
    studies = [
        study("A", primary=TODAY + timedelta(days=30)),
        Study("B", completion=TODAY + timedelta(days=10)),  # no primary date: study date
        study("C", primary=TODAY - timedelta(days=1)),  # past: left out
        study("D", primary=TODAY),  # today counts
        study("E"),  # undated
        Study("F", primary_completion=date(2026, 11, 1)),  # month-only date stored as the 1st
    ]
    rows = upcoming_completions(studies, TODAY)
    assert [r.nct_id for r in rows] == ["D", "B", "F", "A"]
    assert rows[1].completion == TODAY + timedelta(days=10)


def test_no_studies_gives_empty_values_and_no_trials() -> None:
    panel = trial_panel([], TODAY)
    assert panel.next_milestone is None and panel.pipeline_total == 0
    assert panel.phase_chips == [] and panel.indications == [] and panel.upcoming == []
    assert panel.drug == "" and panel.quarters_out == ""
    assert panel.reason_codes == ["no_trials"]


def test_the_panel_follows_the_chosen_study() -> None:
    studies = [
        study(
            "A",
            "PHASE3",
            TODAY + timedelta(days=182),
            interventions=({"type": "DRUG", "name": "DRUG: Zimura"},),
        ),
        study("B", "PHASE2", TODAY + timedelta(days=5)),
    ]
    panel = trial_panel(studies, TODAY)
    assert panel.drug == "Zimura" and panel.quarters_out == "2 quarters out"
    assert panel.reason_codes == []


def test_the_result_depends_on_today_only() -> None:
    studies = [study("A", "PHASE3", date(2026, 12, 1))]
    assert trial_panel(studies, date(2026, 10, 12)).quarters_out == "1 quarter out"
    assert trial_panel(studies, date(2027, 1, 1)).quarters_out == "Past due"


@pytest.mark.django_db
def test_latest_studies_uses_each_studys_newest_version_on_the_day() -> None:
    company = f.company()
    f.ct_study(company, nct_id="NCT00000001", observed_on=date(2026, 9, 1), status="RECRUITING",
               conditions=["A"], highest_phase="PHASE3", content_hash="h1")  # fmt: skip
    f.ct_study(company, nct_id="NCT00000001", observed_on=date(2026, 10, 1), status="TERMINATED",
               conditions=["A"], highest_phase="PHASE3", content_hash="h2")  # fmt: skip
    f.ct_study(company, nct_id="NCT00000002", observed_on=date(2026, 11, 1), content_hash="h3")
    f.ct_study(f.company(), nct_id="NCT00000003", content_hash="h4")  # another company
    on_oct = latest_studies(company.pk, TODAY)
    assert [(s.nct_id, s.status) for s in on_oct] == [("NCT00000001", "TERMINATED")]
    assert len(latest_studies(company.pk, date(2026, 11, 5))) == 2
    assert latest_studies(company.pk, date(2026, 8, 1)) == []
