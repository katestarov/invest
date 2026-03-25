import type { MetricCard } from "../types";

type Props = {
  items: MetricCard[];
};

export function MetricGridSafe({ items }: Props) {
  return (
    <div className="metric-grid">
      {items.map((item) => {
        const comparisonLabel =
          item.comparison_label ??
          (item.value !== null && item.benchmark !== null
            ? item.direction === "higher_better"
              ? item.value >= item.benchmark
                ? "Лучше peers"
                : "Слабее peers"
              : item.value <= item.benchmark
                ? "Лучше peers"
                : "Слабее peers"
            : "Недостаточно данных");
        const tone = comparisonLabel === "Лучше peers" ? "up" : comparisonLabel === "Слабее peers" ? "down" : "neutral";
        const displayValue = item.display_value ?? (item.value !== null ? `${item.value}` : "N/A");
        const displayBenchmark = item.display_benchmark ?? (item.benchmark !== null ? `${item.benchmark}` : "N/A");
        const valueLabel = displayValue === "N/A" ? displayValue : `${displayValue}${item.unit}`;
        const benchmarkLabel = displayBenchmark === "N/A" ? displayBenchmark : `${displayBenchmark}${item.unit}`;

        return (
          <article key={item.label} className="metric-card">
            <div className="metric-card-top">
              <span>{item.label}</span>
              <strong className={tone}>{comparisonLabel}</strong>
            </div>
            <div className="metric-card-value">{valueLabel}</div>
            <div className="metric-card-benchmark">Среднее по группе: {benchmarkLabel}</div>
            <p>{item.description}</p>
          </article>
        );
      })}
    </div>
  );
}
