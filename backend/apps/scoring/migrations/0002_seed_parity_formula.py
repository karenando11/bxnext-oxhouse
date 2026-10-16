"""Seed the parity formula version (the existing system's constants, quirks included).

The values are a snapshot, not an import of live code, so this migration builds the same
database whenever it runs. A test compares the snapshot with ``scoring_engine.PARITY``: if the
defaults change, publish a new formula version instead of editing this one.
"""

import json

from django.db import migrations
from django.utils import timezone

PARITY = json.loads(
    r"""
{
 "config": {
  "cash_health": {
   "cash_to_price_buckets": [
    [
     0.8,
     10.0
    ],
    [
     0.5,
     8.0
    ],
    [
     0.3,
     6.0
    ],
    [
     0.1,
     4.0
    ]
   ],
   "cash_to_price_floor_score": 2.0,
   "cash_to_price_weight": 1.0,
   "dampener_bands": [
    [
     24.0,
     0.5
    ],
    [
     18.0,
     0.65
    ],
    [
     12.0,
     0.8
    ]
   ],
   "days_per_month": 30.0,
   "debt_to_equity_buckets": [
    [
     0.1,
     10.0
    ],
    [
     0.3,
     8.0
    ],
    [
     0.5,
     6.0
    ],
    [
     1.0,
     4.0
    ],
    [
     2.0,
     2.0
    ]
   ],
   "debt_to_equity_ceiling_score": 0.0,
   "debt_to_equity_weight": 0.8,
   "factor_active_late_stage": 1.0,
   "factor_near": 1.0,
   "factor_no_catalyst": 0.5,
   "fallback_lapse_days": 90,
   "no_data_score": 5.0,
   "pcash_buckets": [
    [
     2.0,
     9.0
    ],
    [
     5.0,
     7.0
    ],
    [
     10.0,
     5.0
    ],
    [
     20.0,
     3.0
    ]
   ],
   "pcash_ceiling_score": 1.0,
   "pcash_fallback_below": 1.5,
   "pcash_weight": 1.0,
   "quick_ratio_buckets": [
    [
     5.0,
     10.0
    ],
    [
     3.0,
     8.0
    ],
    [
     1.5,
     6.0
    ],
    [
     1.0,
     4.0
    ],
    [
     0.5,
     2.0
    ]
   ],
   "quick_ratio_floor_score": 0.0,
   "quick_ratio_weight": 1.5,
   "runway_caps": [
    [
     6.0,
     3.0
    ],
    [
     12.0,
     5.0
    ],
    [
     18.0,
     7.0
    ]
   ]
  },
  "composite": {
   "cash_weight": 0.3,
   "divergence_max": 2.0,
   "divergence_min": -2.0,
   "divergence_scale": 0.5,
   "divergence_weight": 0.1,
   "maximum": 10.0,
   "milestone_weight": 0.35,
   "minimum": 0.0,
   "outlier_weight": 0.15,
   "recency_scale": 10.0,
   "recency_weight": 0.1
  },
  "milestone": {
   "active_phase_2_cap": 9.0,
   "active_phase_2_per_trial": 3.0,
   "active_phase_2_weight": 1.0,
   "active_phase_3_cap": 10.0,
   "active_phase_3_per_trial": 4.0,
   "active_phase_3_weight": 1.5,
   "boost_bands": [
    [
     12.0,
     3.0
    ],
    [
     6.0,
     2.0
    ],
    [
     3.0,
     1.5
    ]
   ],
   "boost_fallback": 1.0,
   "clinical_other_peak": 9.0,
   "clinical_other_weight": 2.0,
   "clinical_phase_3_label": "phase_3",
   "clinical_phase_3_peak": 10.0,
   "clinical_phase_3_weight": 3.0,
   "completion_peak": 9.0,
   "completion_weight": 1.0,
   "days_per_month": 30.0,
   "decay_after_days": 540.0,
   "decay_far_score": 1.0,
   "decay_overdue": [
    5.0,
    2.0
   ],
   "decay_rows": [
    [
     30.0,
     2.0,
     3.0
    ],
    [
     90.0,
     1.0,
     5.0
    ],
    [
     180.0,
     0.5,
     7.0
    ]
   ],
   "decay_two_year_days": 730.0,
   "decay_two_year_score": 2.0,
   "decay_year_after": [
    5.0,
    4.0
   ],
   "decay_year_days": 365.0,
   "earnings_bands": [
    [
     14.0,
     10.0
    ],
    [
     30.0,
     8.0
    ],
    [
     60.0,
     6.0
    ],
    [
     90.0,
     4.0
    ]
   ],
   "earnings_far_score": 2.0,
   "earnings_weight_with_clinical": 1.0,
   "earnings_weight_without_clinical": 2.0,
   "eps_next_buckets": [
    [
     50.0,
     8.0
    ],
    [
     20.0,
     6.0
    ],
    [
     0.0,
     4.0
    ]
   ],
   "eps_next_fallback": 2.0,
   "eps_next_weight": 0.8,
   "eps_this_buckets": [
    [
     50.0,
     7.0
    ],
    [
     20.0,
     5.0
    ],
    [
     0.0,
     3.0
    ]
   ],
   "eps_this_fallback": 1.0,
   "eps_this_weight": 0.5,
   "floor_score": 3.0,
   "lapse_days": 90.0,
   "maximum": 10.0,
   "minimum": 0.0,
   "recruiting_score": 7.0,
   "recruiting_status": "RECRUITING",
   "recruiting_weight": 0.5,
   "rel_vol_buckets": [
    [
     5.0,
     10.0
    ],
    [
     3.0,
     8.0
    ],
    [
     2.0,
     6.0
    ],
    [
     1.5,
     4.0
    ]
   ],
   "rel_vol_fallback": 2.0,
   "rel_vol_weight": 0.5
  },
  "runway": {
   "days_per_month": 30.44
  },
  "topology": {
   "bucket_cutoffs": [
    [
     "Nano",
     25.0
    ],
    [
     "Micro",
     100.0
    ],
    [
     "Small",
     500.0
    ]
   ],
   "dimensions": [
    "perf_ytd",
    "perf_m",
    "perf_w",
    "rsi",
    "inst_own",
    "short_float",
    "vol_m"
   ],
   "divergence_other_threshold": 1.0,
   "divergence_other_weight": 0.3,
   "min_peers": 10,
   "min_values": 5,
   "missing_bucket": "?",
   "outlier_max": 10.0,
   "outlier_no_data": 5.0,
   "outlier_scale": 2.5,
   "outlier_sparse": 7.0,
   "recency_month": "perf_m",
   "recency_month_value": 0.75,
   "recency_sparse": 1.0,
   "recency_stale_value": 0.3,
   "recency_threshold": 1.0,
   "recency_week": "perf_w",
   "recency_week_value": 1.0,
   "recency_ytd": "perf_ytd",
   "recency_ytd_value": 0.5,
   "signed_dimensions": [
    "perf_ytd",
    "perf_m",
    "perf_w",
    "rsi",
    "inst_own"
   ],
   "top_bucket": "Mid",
   "unknown_industry": "Unknown"
  }
 },
 "quirks": {
  "carry_forward": false,
  "earnings_past_is_imminent": true,
  "ocf_ytd_naive_sum": true,
  "runway_as_filed": true,
  "trial_burn_on_profitable": true
 }
}
"""
)


def seed(apps, schema_editor):
    FormulaVersion = apps.get_model("scoring", "FormulaVersion")
    FormulaVersion.objects.get_or_create(
        name="parity",
        version=1,
        defaults={
            "effective_from": timezone.now(),
            "config": PARITY["config"],
            "quirks": PARITY["quirks"],
            "notes": "Reproduces the existing system, quirks included. Carry-forward is off.",
        },
    )


def unseed(apps, schema_editor):
    apps.get_model("scoring", "FormulaVersion").objects.filter(name="parity", version=1).delete()


class Migration(migrations.Migration):
    dependencies = [("scoring", "0001_initial")]

    operations = [migrations.RunPython(seed, unseed)]
