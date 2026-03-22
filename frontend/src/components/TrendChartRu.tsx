import type { FundamentalTrendPoint } from "../types";

type Props = {
  points: FundamentalTrendPoint[];
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

export function TrendChartRu({ points }: Props) {
  const width = 480;
  const height = 240;
  const padding = 28;
  const revenue = points.map((point) => point.revenue_bln);
  const fcf = points.map((point) => point.free_cash_flow_bln);

  return (
    <div className="panel">
      <div className="panel-head">
        <h3>История фундаментальных показателей</h3>
        <span>Выручка и свободный денежный поток</span>
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} className="trend-chart">
        <path d={buildPath(revenue, width, height, padding)} className="trend-line revenue" />
        <path d={buildPath(fcf, width, height, padding)} className="trend-line fcf" />
        {points.map((point, index) => (
          <text
            key={point.period}
            x={padding + (index * (width - padding * 2)) / Math.max(points.length - 1, 1)}
            y={height - 8}
            textAnchor="middle"
            className="trend-label"
          >
            {point.period}
          </text>
        ))}
      </svg>
      <div className="legend">
        <span><i className="revenue" />Выручка</span>
        <span><i className="fcf" />Свободный денежный поток</span>
      </div>
    </div>
  );
}

