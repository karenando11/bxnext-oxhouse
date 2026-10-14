"""Composite: the single ranking score, a blend of the component scores.

Pure Python. The blend uses unrounded components; display rounding is applied afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CompositeConfig:
    """Every constant used by the Composite. Step 4 loads these from the formula version."""

    cash_weight: float = 0.30
    milestone_weight: float = 0.35
    outlier_weight: float = 0.15
    recency_weight: float = 0.10
    recency_scale: float = 10.0  # Recency (0.3 to 1.0) is scaled onto 0..10 before weighting
    divergence_weight: float = 0.10
    divergence_scale: float = 0.5
    divergence_min: float = -2.0
    divergence_max: float = 2.0
    minimum: float = 0.0
    maximum: float = 10.0


DEFAULT_CONFIG = CompositeConfig()


@dataclass(frozen=True)
class CompositeResult:
    score: float  # unrounded
    cash_part: float
    milestone_part: float
    outlier_part: float
    recency_part: float
    divergence_part: float
    divergence_term: float  # clamp(divergence * scale, min, max), before weighting
    raw: float  # sum of the parts before the 0..10 clamp


def composite(
    cash_health: float,
    milestone: float,
    outlier: float,
    recency: float,
    divergence: float,
    config: CompositeConfig = DEFAULT_CONFIG,
) -> CompositeResult:
    """Composite of the five unrounded components, with each weighted part returned."""
    divergence_term = min(
        max(divergence * config.divergence_scale, config.divergence_min), config.divergence_max
    )
    cash_part = config.cash_weight * cash_health
    milestone_part = config.milestone_weight * milestone
    outlier_part = config.outlier_weight * outlier
    recency_part = config.recency_weight * (recency * config.recency_scale)
    divergence_part = config.divergence_weight * divergence_term
    raw = cash_part + milestone_part + outlier_part + recency_part + divergence_part
    return CompositeResult(
        score=min(max(raw, config.minimum), config.maximum),
        cash_part=cash_part,
        milestone_part=milestone_part,
        outlier_part=outlier_part,
        recency_part=recency_part,
        divergence_part=divergence_part,
        divergence_term=divergence_term,
        raw=raw,
    )
