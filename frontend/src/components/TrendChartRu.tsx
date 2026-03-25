import type { FundamentalTrendPoint } from "../types";

type Props = {
  points: FundamentalTrendPoint[];
};

function buildPath(values: number[], width: number, height: number, padding: number) {
  if (!values.length) {
    return "";
  }
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

function buildIndexedPath(
  points: Array<{ index: number; value: number }>,
  totalCount: number,
  width: number,
  height: number,
  padding: number,
) {
  if (!points.length) {
    return "";
  }

  const values = points.map((point) => point.value);
  const max = Math.max(...values);
  const min = Math.min(...values);
  const range = max - min || 1;

  return points
    .map((point, pointIndex) => {
      const x = padding + (point.index * (width - padding * 2)) / Math.max(totalCount - 1, 1);
      const y = height - padding - ((point.value - min) / range) * (height - padding * 2);
      return `${pointIndex === 0 ? "M" : "L"} ${x} ${y}`;
    })
    .join(" ");
}

export function TrendChartRu({ points }: Props) {
  const width = 480;
  const height = 240;
  const padding = 28;
  const sortedPoints = [...points].sort((left, right) =>
    left.period.localeCompare(right.period, undefined, { numeric: true }),
  );
  const revenue = sortedPoints.map((point) => point.revenue_bln);
  const fcf = sortedPoints
    .map((point, index) => (point.free_cash_flow_bln === null ? null : { index, value: point.free_cash_flow_bln }))
    .filter((point): point is { index: number; value: number } => point !== null);

  return (
    <div className="panel">
      <div className="panel-head">
        <h3>История фундаментальных показателей</h3>
        <span>Выручка и свободный денежный поток</span>
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} className="trend-chart">
        {revenue.length > 0 && <path d={buildPath(revenue, width, height, padding)} className="trend-line revenue" />}
        {fcf.length > 0 && <path d={buildIndexedPath(fcf, sortedPoints.length, width, height, padding)} className="trend-line fcf" />}
        {sortedPoints.map((point, index) => (
          <text
            key={point.period}
            x={padding + (index * (width - padding * 2)) / Math.max(sortedPoints.length - 1, 1)}
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

