import type { MetricCard } from "../types";

type Props = {
  items: MetricCard[];
};

export function MetricGridRu({ items }: Props) {
  return (
    <div className="metric-grid">
      {items.map((item) => {
        const isPositive =
          item.direction === "higher_better"
            ? item.value >= item.benchmark
            : item.value <= item.benchmark;

        return (
          <article key={item.label} className="metric-card">
            <div className="metric-card-top">
              <span>{item.label}</span>
              <strong className={isPositive ? "up" : "down"}>
                {isPositive ? "Лучше peers" : "Слабее peers"}
              </strong>
            </div>
            <div className="metric-card-value">
              {item.value}
              {item.unit}
            </div>
            <div className="metric-card-benchmark">
              Среднее по группе: {item.benchmark}
              {item.unit}
            </div>
            <p>{item.description}</p>
          </article>
        );
      })}
    </div>
  );
}

