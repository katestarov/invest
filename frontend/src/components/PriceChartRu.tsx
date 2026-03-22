import type { PriceHistoryPoint } from "../types";

type Props = {
  points: PriceHistoryPoint[];
};

function buildPath(values: number[], width: number, height: number, padding: number) {
  const max = Math.max(...values);
  const min = Math.min(...values);
  const range = max - min || 1;

  return values
    .map((value, index) => {
      const x = padding + (index * (width - padding * 2)) / Math.max(values.length - 1, 1);
      const y = height - padding - ((value - min) / range) * (height - padding * 2);
      return `${index === 0 ? "M" : "L"} ${x} ${y}`;
    })
    .join(" ");
}

export function PriceChartRu({ points }: Props) {
  const width = 480;
  const height = 240;
  const padding = 28;

  if (!points.length) {
    return null;
  }

  const closes = points.map((point) => point.close);
  const labels = [points[0], points[Math.floor(points.length / 2)], points[points.length - 1]].filter(Boolean);

  return (
    <div className="panel">
      <div className="panel-head">
        <h3>История цены акции</h3>
        <span>Последние 24 месячные точки</span>
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} className="trend-chart">
        <path d={buildPath(closes, width, height, padding)} className="trend-line revenue" />
        {labels.map((point, index) => (
          <text
            key={`${point.date}-${index}`}
            x={padding + (points.indexOf(point) * (width - padding * 2)) / Math.max(points.length - 1, 1)}
            y={height - 8}
            textAnchor="middle"
            className="trend-label"
          >
            {point.date.slice(0, 7)}
          </text>
        ))}
      </svg>
    </div>
  );
}

