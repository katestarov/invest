from __future__ import annotations

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
    if total_weight <= 0:
        return 0.0
    valid_weight = sum(weight for value, weight in components if value is not None and weight > 0)
    return valid_weight / total_weight


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
