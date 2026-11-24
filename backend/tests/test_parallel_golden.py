"""The diff report run on the golden day against its own export (real data; skipped without it).

The golden export is one day of the current system. Comparing it with this system's scoring of
the same inputs shows what a real parallel-run day will look like. Known causes of difference:
the export has ``eps_this`` blank (the old system had it when it scored), the golden loader does
not carry the latest trial or the overdue flag, and 93 earnings dates are blank in the load.
"""

from __future__ import annotations

import collections
from pathlib import Path
from typing import Any

import pytest

from apps.platform.parallel import UNEXPLAINED, compare_day, read_export
from tests.conftest import GOLDEN_DAY


@pytest.mark.golden
@pytest.mark.django_db
def test_the_golden_day_against_its_own_export(
    golden_path: Path, golden_day: tuple[Any, Any]
) -> None:
    report = compare_day(GOLDEN_DAY, read_export(golden_path))
    assert report["export_companies"] == report["our_companies"] == report["compared"] == 1053
    assert report["only_in_export_n"] == report["only_here_n"] == 0
    assert report["within_tolerance"] == 621  # the rows whose milestone does not need eps_this
    fields = collections.Counter(
        d["field"] for d in (*report["score_diffs"], *report["field_diffs"])
    )
    assert set(fields) <= {
        "milestone_proximity",
        "ct_latest_trial_nct",
        "ct_latest_trial_status",
        "earnings_date",
        "catalyst_clinical_overdue",
    }
    # Counts, ratios, runway (months, quarters and rating) and peer groups all agree.
    unexplained = [
        d for d in (*report["score_diffs"], *report["field_diffs"]) if d["class"] == UNEXPLAINED
    ]
    assert {d["field"] for d in unexplained} == {"milestone_proximity"}
