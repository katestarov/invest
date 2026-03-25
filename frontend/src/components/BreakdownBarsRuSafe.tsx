import type { ScoreBreakdownItem } from "../types";

type Props = {
  items: ScoreBreakdownItem[];
};

export function BreakdownBarsRuSafe({ items }: Props) {
  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Структура итоговой оценки</h3>
        <span>Вес каждого блока</span>
      </div>
      <div className="breakdown-list">
        {items.map((item) => (
          <div key={item.key} className="breakdown-item">
            <div className="breakdown-meta">
              <strong>{item.label}</strong>
              <span>{(item.weight * 100).toFixed(1)}%</span>
            </div>
            <div className="breakdown-bar">
              <div style={{ width: `${item.score ?? 0}%`, opacity: item.score === null ? 0.35 : 1 }} />
            </div>
            <div className="breakdown-meta">
              <span>{item.summary}</span>
              <strong>{item.score === null ? "N/A" : item.score.toFixed(1)}</strong>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
