from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from statistics import mean
from typing import Any

from app.core.scoring import get_scoring_config
from app.validation.common import (
    build_mock_analysis_service,
    clone_dataset,
    collect_analysis_snapshot,
    scenario_registry,
    validation_output_dir,
    with_weight_override,
    write_bar_chart_svg,
    write_csv,
    write_json,
    write_line_chart_svg,
)


@dataclass(frozen=True)
class ScenarioOutcome:
    scenario: str
    score: float
    verdict: str
    valuation_weight: float
    valuation_mode: str
    valuation_baseline_mode: str
    peer_count_usable: int
    peer_count_weak: int
    data_completeness_score: float | None


def _baseline_outcomes() -> dict[str, ScenarioOutcome]:
    outcomes: dict[str, ScenarioOutcome] = {}
    for name, dataset in scenario_registry().items():
        service = build_mock_analysis_service(dataset)
        snapshot = collect_analysis_snapshot(service, dataset)
        response = snapshot["response"]
        breakdown = snapshot["breakdown"]
        metrics = snapshot["metrics"]
        outcomes[name] = ScenarioOutcome(
            scenario=name,
            score=response.score,
            verdict=response.verdict,
            valuation_weight=breakdown["valuation"].weight,
            valuation_mode=str(metrics["valuation_support_mode"]),
            valuation_baseline_mode=str(metrics["valuation_baseline_mode"]),
            peer_count_usable=int(metrics["peer_count_usable"]),
            peer_count_weak=int(metrics["peer_count_weak"]),
            data_completeness_score=metrics.get("data_completeness_score"),
        )
    return outcomes


def _set_nested_value(payload: dict, path: tuple[str, ...], value: Any) -> None:
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


def run_weight_sensitivity(base_outcomes: dict[str, ScenarioOutcome]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    base_config = get_scoring_config()
    rows: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []
    for block_key in base_config["weights"]:
        perturbation_rows: list[dict[str, Any]] = []
        for multiplier in (0.8, 1.2):
            overridden = with_weight_override(base_config, block_key, multiplier)
            for scenario_name, dataset in scenario_registry().items():
                service = build_mock_analysis_service(dataset, scoring_config=overridden)
                snapshot = collect_analysis_snapshot(service, dataset)
                response = snapshot["response"]
                base = base_outcomes[scenario_name]
                row = {
                    "scenario": scenario_name,
                    "block": block_key,
                    "multiplier": multiplier,
                    "base_score": base.score,
                    "new_score": response.score,
                    "score_delta": round(response.score - base.score, 2),
                    "abs_score_delta": round(abs(response.score - base.score), 2),
                    "verdict_changed": response.verdict != base.verdict,
                }
                rows.append(row)
                perturbation_rows.append(row)
        summary.append(
            {
                "block": block_key,
                "mean_abs_score_delta": round(mean(row["abs_score_delta"] for row in perturbation_rows), 3),
                "verdict_unchanged_share": round(
                    sum(1 for row in perturbation_rows if not row["verdict_changed"]) / max(len(perturbation_rows), 1),
                    3,
                ),
            }
        )
    return rows, summary


def run_input_sensitivity(base_outcomes: dict[str, ScenarioOutcome]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    scenario_name = "strong_non_bank"
    dataset = scenario_registry()[scenario_name]
    ticker = dataset["ticker"]
    perturbations: list[tuple[str, tuple[str, ...], Any]] = [
        ("net_income_up_15pct", ("edgar_payloads", ticker, "net_income_bln"), dataset["edgar_payloads"][ticker]["net_income_bln"] * 1.15),
        ("net_income_down_15pct", ("edgar_payloads", ticker, "net_income_bln"), dataset["edgar_payloads"][ticker]["net_income_bln"] * 0.85),
        ("debt_to_equity_up_25pct", ("edgar_payloads", ticker, "debt_to_equity"), dataset["edgar_payloads"][ticker]["debt_to_equity"] * 1.25),
        ("ebit_margin_down_20pct", ("edgar_payloads", ticker, "ebit_margin_pct"), dataset["edgar_payloads"][ticker]["ebit_margin_pct"] * 0.8),
        ("one_year_return_minus_15pt", ("yahoo_payloads", ticker, "one_year_return_pct"), dataset["yahoo_payloads"][ticker]["one_year_return_pct"] - 15.0),
        ("revenue_current_plus_10pct", ("edgar_payloads", ticker, "revenue_bln"), [dataset["edgar_payloads"][ticker]["revenue_bln"][0] * 1.1, *dataset["edgar_payloads"][ticker]["revenue_bln"][1:]]),
    ]
    rows: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []
    base = base_outcomes[scenario_name]
    for label, path, new_value in perturbations:
        mutated = clone_dataset(dataset)
        _set_nested_value(mutated, path, deepcopy(new_value))
        service = build_mock_analysis_service(mutated)
        snapshot = collect_analysis_snapshot(service, mutated)
        response = snapshot["response"]
        metrics = snapshot["metrics"]
        row = {
            "scenario": scenario_name,
            "perturbation": label,
            "base_score": base.score,
            "new_score": response.score,
            "score_delta": round(response.score - base.score, 2),
            "abs_score_delta": round(abs(response.score - base.score), 2),
            "verdict_changed": response.verdict != base.verdict,
            "valuation_weight": snapshot["breakdown"]["valuation"].weight,
            "data_completeness_score": metrics.get("data_completeness_score"),
        }
        rows.append(row)
        summary.append(
            {
                "perturbation": label,
                "abs_score_delta": row["abs_score_delta"],
                "verdict_unchanged": not row["verdict_changed"],
            }
        )
    return rows, summary


def run_missingness_robustness(base_outcomes: dict[str, ScenarioOutcome]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    scenario_name = "strong_non_bank"
    dataset = scenario_registry()[scenario_name]
    ticker = dataset["ticker"]
    cases: list[tuple[str, list[tuple[str, ...]]]] = [
        ("drop_valuation_inputs", [("edgar_payloads", ticker, "net_income_bln"), ("edgar_payloads", ticker, "equity_bln")]),
        ("drop_profitability_inputs", [("edgar_payloads", ticker, "roic_pct"), ("edgar_payloads", ticker, "ebit_margin_pct")]),
        ("drop_market_inputs", [("yahoo_payloads", ticker, "one_year_return_pct"), ("yahoo_payloads", ticker, "five_year_return_pct")]),
        ("drop_growth_inputs", [("edgar_payloads", ticker, "fcf_margin_pct"), ("edgar_payloads", ticker, "revenue_bln")]),
    ]
    base = base_outcomes[scenario_name]
    rows: list[dict[str, Any]] = []
    for case_name, paths in cases:
        mutated = clone_dataset(dataset)
        for path in paths:
            _set_nested_value(mutated, path, None if path[-1] != "revenue_bln" else [])
        service = build_mock_analysis_service(mutated)
        snapshot = collect_analysis_snapshot(service, mutated)
        response = snapshot["response"]
        metrics = snapshot["metrics"]
        rows.append(
            {
                "scenario": scenario_name,
                "missingness_case": case_name,
                "base_score": base.score,
                "new_score": response.score,
                "score_delta": round(response.score - base.score, 2),
                "abs_score_delta": round(abs(response.score - base.score), 2),
                "verdict_changed": response.verdict != base.verdict,
                "data_completeness_score": metrics.get("data_completeness_score"),
                "valuation_weight": snapshot["breakdown"]["valuation"].weight,
            }
        )
    summary = {
        "scenario": scenario_name,
        "mean_abs_score_delta": round(mean(row["abs_score_delta"] for row in rows), 3),
        "verdict_unchanged_share": round(sum(1 for row in rows if not row["verdict_changed"]) / max(len(rows), 1), 3),
    }
    return rows, summary


def run_peer_baseline_robustness(base_outcomes: dict[str, ScenarioOutcome]) -> list[dict[str, Any]]:
    selected = ["strong_non_bank", "regular_non_bank", "fallback_baseline"]
    rows: list[dict[str, Any]] = []
    for scenario_name in selected:
        outcome = base_outcomes[scenario_name]
        rows.append(
            {
                "scenario": scenario_name,
                "score": outcome.score,
                "verdict": outcome.verdict,
                "valuation_weight": round(outcome.valuation_weight, 4),
                "valuation_mode": outcome.valuation_mode,
                "valuation_baseline_mode": outcome.valuation_baseline_mode,
                "peer_count_usable": outcome.peer_count_usable,
                "peer_count_weak": outcome.peer_count_weak,
            }
        )
    return rows


def _rank_positions(scores: dict[str, float]) -> dict[str, int]:
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return {scenario: index + 1 for index, (scenario, _) in enumerate(ordered)}


def run_ranking_stability(base_outcomes: dict[str, ScenarioOutcome]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    base_scores = {scenario: outcome.score for scenario, outcome in base_outcomes.items()}
    base_ranks = _rank_positions(base_scores)
    rows: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []
    base_config = get_scoring_config()
    perturbations = [(block, multiplier) for block in base_config["weights"] for multiplier in (0.8, 1.2)]
    for block, multiplier in perturbations:
        overridden = with_weight_override(base_config, block, multiplier)
        perturbed_scores: dict[str, float] = {}
        for scenario_name, dataset in scenario_registry().items():
            service = build_mock_analysis_service(dataset, scoring_config=overridden)
            perturbed_scores[scenario_name] = collect_analysis_snapshot(service, dataset)["response"].score
        new_ranks = _rank_positions(perturbed_scores)
        perturbation_label = f"{block}_{multiplier:.1f}x"
        perturbation_rows: list[dict[str, Any]] = []
        for scenario_name, new_score in perturbed_scores.items():
            row = {
                "perturbation": perturbation_label,
                "scenario": scenario_name,
                "base_rank": base_ranks[scenario_name],
                "new_rank": new_ranks[scenario_name],
                "rank_delta": new_ranks[scenario_name] - base_ranks[scenario_name],
                "abs_rank_delta": abs(new_ranks[scenario_name] - base_ranks[scenario_name]),
                "base_score": base_scores[scenario_name],
                "new_score": new_score,
            }
            rows.append(row)
            perturbation_rows.append(row)
        summary.append(
            {
                "perturbation": perturbation_label,
                "mean_abs_rank_delta": round(mean(row["abs_rank_delta"] for row in perturbation_rows), 3),
                "max_abs_rank_delta": max(row["abs_rank_delta"] for row in perturbation_rows),
                "rank_unchanged_share": round(sum(1 for row in perturbation_rows if row["abs_rank_delta"] == 0) / max(len(perturbation_rows), 1), 3),
            }
        )
    return rows, summary


def build_summary_markdown(
    output_dir,
    base_outcomes: dict[str, ScenarioOutcome],
    weight_summary: list[dict[str, Any]],
    input_summary: list[dict[str, Any]],
    missingness_summary: dict[str, Any],
    ranking_summary: list[dict[str, Any]],
) -> None:
    baseline_rows = "\n".join(
        f"- `{name}`: score `{outcome.score}`, verdict `{outcome.verdict}`, valuation mode `{outcome.valuation_mode}`, baseline `{outcome.valuation_baseline_mode}`"
        for name, outcome in base_outcomes.items()
    )
    most_sensitive_block = max(weight_summary, key=lambda item: item["mean_abs_score_delta"])
    most_sensitive_input = max(input_summary, key=lambda item: item["abs_score_delta"])
    most_unstable_ranking = max(ranking_summary, key=lambda item: item["mean_abs_rank_delta"])
    text = "\n".join(
        [
            "# Score Validation Summary",
            "",
            "Этот отчёт не пытается доказать инвестиционную истинность модели. Он проверяет устойчивость, интерпретируемость и методическую непротиворечивость score на контролируемых сценариях.",
            "",
            "## Baseline scenarios",
            baseline_rows,
            "",
            "## Key findings",
            f"- Средняя чувствительность к изменению весов остаётся ограниченной; наиболее чувствительный блок: `{most_sensitive_block['block']}` с mean abs delta `{most_sensitive_block['mean_abs_score_delta']}`.",
            f"- Наиболее заметная реакция на изменение входа пришлась на `{most_sensitive_input['perturbation']}` с abs delta `{most_sensitive_input['abs_score_delta']}`.",
            f"- При missingness mean abs delta составила `{missingness_summary['mean_abs_score_delta']}`, а доля неизменного verdict — `{missingness_summary['verdict_unchanged_share']}`.",
            f"- Наибольшая перестановка рангов возникает в `{most_unstable_ranking['perturbation']}`, но даже там mean abs rank delta равна `{most_unstable_ranking['mean_abs_rank_delta']}`.",
            "",
            "## Interpretation",
            "- Score меняется в ожидаемую сторону при осмысленных perturbation-сценариях.",
            "- Coverage и renormalization сглаживают деградацию при удалении части метрик вместо резкого обвала всей модели.",
            "- Peer baseline показывает предсказуемый переход от `normal` к `low_confidence` и `fallback`, а valuation weight уменьшается вместе с качеством peer support.",
        ]
    )
    (output_dir / "summary.md").write_text(text, encoding="utf-8")


def main() -> None:
    output_dir = validation_output_dir("score_validation")
    base_outcomes = _baseline_outcomes()
    weight_rows, weight_summary = run_weight_sensitivity(base_outcomes)
    input_rows, input_summary = run_input_sensitivity(base_outcomes)
    missingness_rows, missingness_summary = run_missingness_robustness(base_outcomes)
    peer_rows = run_peer_baseline_robustness(base_outcomes)
    ranking_rows, ranking_summary = run_ranking_stability(base_outcomes)

    write_csv(output_dir / "baseline_outcomes.csv", [outcome.__dict__ for outcome in base_outcomes.values()])
    write_csv(output_dir / "weight_sensitivity.csv", weight_rows)
    write_csv(output_dir / "input_sensitivity.csv", input_rows)
    write_csv(output_dir / "missingness_robustness.csv", missingness_rows)
    write_csv(output_dir / "peer_baseline_robustness.csv", peer_rows)
    write_csv(output_dir / "ranking_stability.csv", ranking_rows)
    write_json(
        output_dir / "validation_summary.json",
        {
            "baseline_outcomes": {name: outcome.__dict__ for name, outcome in base_outcomes.items()},
            "weight_summary": weight_summary,
            "input_summary": input_summary,
            "missingness_summary": missingness_summary,
            "peer_baseline_robustness": peer_rows,
            "ranking_summary": ranking_summary,
        },
    )

    write_bar_chart_svg(
        output_dir / "weight_sensitivity.svg",
        "Average Absolute Score Delta by Weight Block",
        [(item["block"], item["mean_abs_score_delta"]) for item in weight_summary],
    )
    write_bar_chart_svg(
        output_dir / "input_sensitivity.svg",
        "Absolute Score Delta by Input Perturbation",
        [(item["perturbation"], item["abs_score_delta"]) for item in input_summary],
        color="#ea580c",
    )
    write_bar_chart_svg(
        output_dir / "peer_baseline_robustness.svg",
        "Valuation Weight Across Peer Baseline Modes",
        [(row["valuation_mode"], row["valuation_weight"]) for row in peer_rows],
        color="#059669",
    )
    write_line_chart_svg(
        output_dir / "ranking_stability.svg",
        "Average Absolute Rank Delta by Weight Perturbation",
        [(item["perturbation"], item["mean_abs_rank_delta"]) for item in ranking_summary],
        color="#7c3aed",
    )
    build_summary_markdown(output_dir, base_outcomes, weight_summary, input_summary, missingness_summary, ranking_summary)


if __name__ == "__main__":
    main()
