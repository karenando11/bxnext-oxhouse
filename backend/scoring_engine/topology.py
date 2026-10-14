"""Peer-relative scores: Outlier, Divergence and Recency.

Pure Python. No Django, no database, no network, no clock. Callers pass plain data and get
plain data back, which keeps the golden regression tests fast and deterministic.

How it works
------------
Companies are grouped into peer groups keyed ``"<industry>|<market cap bucket>"``. Within a
group, each dimension gets a median and a median absolute deviation (MAD, unscaled).
A company's robust z-score on a dimension is ``(value - median) / MAD``; it is 0 when the MAD
is 0. Three scores follow from the z-scores:

* **Outlier**: how unusual the company is, in either direction.
* **Divergence**: which direction the unusualness points.
* **Recency**: how fresh the unusualness is (this week, this month, or year to date).

Groups smaller than ``min_peers`` are "sparse" and receive fixed defaults.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import median

DIMENSIONS: tuple[str, ...] = (
    "perf_ytd",
    "perf_m",
    "perf_w",
    "rsi",
    "inst_own",
    "short_float",
    "vol_m",
)


@dataclass(frozen=True)
class TopologyConfig:
    """Every constant used by the peer-relative scores.

    Step 4 will load these from a versioned formula configuration; the defaults below are the
    values the current system uses.

    Validation status against the 2026-10-12 golden export (1,053 companies). Pinned by the
    data, meaning changing the value breaks stored scores: bucket cutoffs, ``outlier_scale``,
    ``divergence_other_weight``, ``outlier_sparse``, ``recency_stale_value``, and
    ``min_peers`` from below (a group of 9 is sparse). **Not** pinned by the data, taken from
    the system's documentation only: ``min_values`` (no group sits at 4 to 6 values),
    whether a group of exactly ``min_peers`` is sparse (no group has size 10),
    ``outlier_no_data`` and the recency of a company with no usable dimension.
    """

    dimensions: tuple[str, ...] = DIMENSIONS
    # Dimensions that add their full signed z to Divergence. The remaining dimensions add
    # ``divergence_other_weight * |z|`` only when ``|z| > divergence_other_threshold``.
    signed_dimensions: tuple[str, ...] = ("perf_ytd", "perf_m", "perf_w", "rsi", "inst_own")
    divergence_other_weight: float = 0.3
    divergence_other_threshold: float = 1.0

    # Market cap buckets, in USD millions: below the first cutoff is the first bucket, and so on.
    bucket_cutoffs: tuple[tuple[str, float], ...] = (
        ("Nano", 25.0),
        ("Micro", 100.0),
        ("Small", 500.0),
    )
    top_bucket: str = "Mid"
    missing_bucket: str = "?"
    unknown_industry: str = "Unknown"

    min_peers: int = 10  # groups smaller than this are sparse
    min_values: int = 5  # a dimension needs this many values in a group to have statistics

    outlier_scale: float = 2.5
    outlier_max: float = 10.0
    outlier_sparse: float = 7.0
    outlier_no_data: float = 5.0  # non-sparse company with no usable dimension

    recency_week: str = "perf_w"
    recency_month: str = "perf_m"
    recency_ytd: str = "perf_ytd"
    recency_threshold: float = 1.0
    recency_week_value: float = 1.0
    recency_month_value: float = 0.75
    recency_ytd_value: float = 0.5
    recency_stale_value: float = 0.3
    recency_sparse: float = 1.0


DEFAULT_CONFIG = TopologyConfig()


@dataclass(frozen=True)
class DimensionStat:
    """Median and unscaled MAD of one dimension within one peer group."""

    median: float
    mad: float
    n_values: int


@dataclass(frozen=True)
class PeerGroupStat:
    peer_group_key: str
    dimension: str
    n_values: int
    median: float
    mad: float


@dataclass(frozen=True)
class CompanyPeerInput:
    company_id: str
    industry: str | None
    mcap: float | None  # USD millions
    metrics: Mapping[str, float | None]


@dataclass(frozen=True)
class PeerScores:
    outlier: float
    divergence: float
    recency: float


@dataclass(frozen=True)
class TopologyResult:
    company_id: str
    peer_group_key: str
    mcap_bucket: str
    peer_n: int
    is_sparse: bool
    outlier: float
    divergence: float
    recency: float
    z_scores: dict[str, float]  # empty for sparse groups


@dataclass(frozen=True)
class TopologyOutput:
    results: dict[str, TopologyResult]
    group_stats: tuple[PeerGroupStat, ...]


def _finite(value: float | None) -> float | None:
    """Treat None and non-finite numbers (NaN, infinity) as missing."""
    if value is None or not math.isfinite(value):
        return None
    return float(value)


def mcap_bucket(mcap: float | None, config: TopologyConfig = DEFAULT_CONFIG) -> str:
    """Market cap bucket for a market cap in USD millions."""
    value = _finite(mcap)
    if value is None:
        return config.missing_bucket
    for name, cutoff in config.bucket_cutoffs:
        if value < cutoff:
            return name
    return config.top_bucket


def peer_group_key(
    industry: str | None, mcap: float | None, config: TopologyConfig = DEFAULT_CONFIG
) -> str:
    cleaned = (industry or "").strip()
    return f"{cleaned or config.unknown_industry}|{mcap_bucket(mcap, config)}"


def median_and_mad(values: Sequence[float]) -> tuple[float, float]:
    """Median and unscaled median absolute deviation (no 1.4826 factor)."""
    centre = median(values)
    return centre, median(abs(v - centre) for v in values)


def group_statistics(
    group_metrics: Sequence[Mapping[str, float | None]],
    config: TopologyConfig = DEFAULT_CONFIG,
) -> dict[str, DimensionStat]:
    """Statistics for every dimension that has at least ``min_values`` values in the group."""
    stats: dict[str, DimensionStat] = {}
    for dimension in config.dimensions:
        values = [
            v for metrics in group_metrics if (v := _finite(metrics.get(dimension))) is not None
        ]
        if len(values) >= config.min_values:
            centre, mad = median_and_mad(values)
            stats[dimension] = DimensionStat(median=centre, mad=mad, n_values=len(values))
    return stats


def z_scores(
    metrics: Mapping[str, float | None],
    stats: Mapping[str, DimensionStat],
    config: TopologyConfig = DEFAULT_CONFIG,
) -> dict[str, float]:
    """Robust z-score per dimension that has both a value and group statistics."""
    scores: dict[str, float] = {}
    for dimension in config.dimensions:
        stat = stats.get(dimension)
        value = _finite(metrics.get(dimension))
        if stat is None or value is None:
            continue
        scores[dimension] = 0.0 if stat.mad == 0 else (value - stat.median) / stat.mad
    return scores


def recency_from_z(z: Mapping[str, float], config: TopologyConfig = DEFAULT_CONFIG) -> float:
    week = abs(z.get(config.recency_week, 0.0))
    month = abs(z.get(config.recency_month, 0.0))
    ytd = abs(z.get(config.recency_ytd, 0.0))
    threshold = config.recency_threshold
    if week > month and week > threshold:
        return config.recency_week_value
    if month > ytd and month > threshold:
        return config.recency_month_value
    if ytd > threshold:
        return config.recency_ytd_value
    return config.recency_stale_value


def scores_from_z(z: Mapping[str, float], config: TopologyConfig = DEFAULT_CONFIG) -> PeerScores:
    """Outlier, Divergence and Recency for a company in a non-sparse group."""
    if not z:
        return PeerScores(
            outlier=config.outlier_no_data, divergence=0.0, recency=recency_from_z(z, config)
        )
    mean_square = sum(v * v for v in z.values()) / len(z)
    outlier = min(max(math.sqrt(mean_square) * config.outlier_scale, 0.0), config.outlier_max)

    divergence = 0.0
    for dimension, value in z.items():
        if dimension in config.signed_dimensions:
            divergence += value
        elif abs(value) > config.divergence_other_threshold:
            divergence += config.divergence_other_weight * abs(value)

    return PeerScores(outlier=outlier, divergence=divergence, recency=recency_from_z(z, config))


def topology_scores(
    metrics: Mapping[str, float | None],
    stats: Mapping[str, DimensionStat],
    config: TopologyConfig = DEFAULT_CONFIG,
) -> PeerScores:
    """Scores for one company against explicit group statistics (non-sparse path)."""
    return scores_from_z(z_scores(metrics, stats, config), config)


def compute_topology(
    companies: Sequence[CompanyPeerInput], config: TopologyConfig = DEFAULT_CONFIG
) -> TopologyOutput:
    """Score a whole universe.

    Peer groups are formed from the companies passed in, so callers must pass the full
    universe (peer statistics depend on every member), not a filtered subset.
    """
    ids = [company.company_id for company in companies]
    if len(set(ids)) != len(ids):
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError(f"Duplicate company_id values: {duplicates}")

    groups: dict[str, list[CompanyPeerInput]] = defaultdict(list)
    for company in companies:
        groups[peer_group_key(company.industry, company.mcap, config)].append(company)

    results: dict[str, TopologyResult] = {}
    group_stats: list[PeerGroupStat] = []

    for key in sorted(groups):
        members = groups[key]
        peer_n = len(members)
        is_sparse = peer_n < config.min_peers
        stats = {} if is_sparse else group_statistics([m.metrics for m in members], config)
        for dimension in config.dimensions:
            if dimension in stats:
                stat = stats[dimension]
                group_stats.append(
                    PeerGroupStat(key, dimension, stat.n_values, stat.median, stat.mad)
                )

        for member in members:
            bucket = mcap_bucket(member.mcap, config)
            if is_sparse:
                results[member.company_id] = TopologyResult(
                    company_id=member.company_id,
                    peer_group_key=key,
                    mcap_bucket=bucket,
                    peer_n=peer_n,
                    is_sparse=True,
                    outlier=config.outlier_sparse,
                    divergence=0.0,
                    recency=config.recency_sparse,
                    z_scores={},
                )
                continue
            z = z_scores(member.metrics, stats, config)
            scores = scores_from_z(z, config)
            results[member.company_id] = TopologyResult(
                company_id=member.company_id,
                peer_group_key=key,
                mcap_bucket=bucket,
                peer_n=peer_n,
                is_sparse=False,
                outlier=scores.outlier,
                divergence=scores.divergence,
                recency=scores.recency,
                z_scores=z,
            )

    return TopologyOutput(results=results, group_stats=tuple(group_stats))
