from __future__ import annotations

from statistics import median
from typing import TypeAlias

Number: TypeAlias = float | None


def safe_ratio(
    numerator: float | None,
    denominator: float | None,
    *,
    allow_negative_denominator: bool = False,
) -> Number:
    if numerator is None or denominator is None:
        return None
    if denominator == 0:
        return None
    if denominator < 0 and not allow_negative_denominator:
        return None
    return numerator / denominator


def round_or_none(value: Number, digits: int = 2) -> Number:
    if value is None:
        return None
    return round(value, digits)


def premium_pct(value: Number, benchmark: Number) -> Number:
    ratio = safe_ratio(value, benchmark)
    if ratio is None:
        return None
    return (ratio - 1) * 100


def score_positive(value: Number, cap: float) -> Number:
    if value is None or cap <= 0:
        return None
    return min(max(value, 0.0), cap) / cap * 100


def score_inverse(value: Number, cap: float) -> Number:
    if value is None or cap <= 0:
        return None
    return max(0.0, 100 - (min(max(value, 0.0), cap) / cap * 100))


def weighted_score(components: list[tuple[Number, float]]) -> Number:
    valid = [(value, weight) for value, weight in components if value is not None and weight > 0]
    if not valid:
        return None
    total_weight = sum(weight for _, weight in valid)
    if total_weight <= 0:
        return None
    return sum(value * (weight / total_weight) for value, weight in valid)


def coverage_ratio(components: list[tuple[Number, float]]) -> float:
    total_weight = sum(weight for _, weight in components if weight > 0)
    total_count = len([1 for _, weight in components if weight > 0])
    if total_weight <= 0 or total_count <= 0:
        return 0.0
    valid_weight = sum(weight for value, weight in components if value is not None and weight > 0)
    valid_count = len([1 for value, weight in components if value is not None and weight > 0])
    return min(valid_weight / total_weight, valid_count / total_count)


def normalize_weights(values: dict[str, Number], configured_weights: dict[str, float]) -> dict[str, float]:
    valid_total = sum(configured_weights[key] for key, value in values.items() if value is not None)
    if valid_total <= 0:
        return {key: 0.0 for key in configured_weights}
    return {
        key: (configured_weights[key] / valid_total) if values.get(key) is not None else 0.0
        for key in configured_weights
    }


def winsorized_mean(values: list[float], lower_quantile: float = 0.1, upper_quantile: float = 0.9) -> Number:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) < 3:
        return sum(ordered) / len(ordered)
    lower_index = int((len(ordered) - 1) * lower_quantile)
    upper_index = int((len(ordered) - 1) * upper_quantile)
    lower_bound = ordered[lower_index]
    upper_bound = ordered[upper_index]
    clipped = [min(max(value, lower_bound), upper_bound) for value in ordered]
    return sum(clipped) / len(clipped)


def median_or_none(values: list[float]) -> Number:
    if not values:
        return None
    return float(median(values))


def robust_baseline(
    values: list[float],
    *,
    prefer_median: bool = False,
    min_count: int = 3,
) -> tuple[Number, int, bool]:
    if not values:
        return None, 0, True

    baseline = median_or_none(values) if prefer_median else winsorized_mean(values)
    if baseline is None:
        return None, 0, True

    ordered = sorted(values)
    midpoint = median_or_none(values) or baseline
    spread_ratio = (ordered[-1] / max(ordered[0], 0.0001)) if ordered[0] > 0 else float("inf")
    noisy = len(values) < min_count or (midpoint > 0 and abs(baseline - midpoint) / midpoint > 0.35) or spread_ratio > 6
    return baseline, len(values), noisy


def apply_low_confidence_cap(score: Number, coverage: float) -> Number:
    if score is None:
        return None
    if coverage >= 0.5:
        return score
    max_score = 60 + (coverage * 50)
    return min(score, max_score)


def is_bank_like_company(sector: str | None, industry: str | None) -> bool:
    sector_text = (sector or "").lower()
    industry_text = (industry or "").lower()
    return sector_text == "financial services" and any(
        fragment in industry_text
        for fragment in ("bank", "banc", "financial", "credit", "lending", "savings", "commercial")
    )
