import type { ScoreBreakdownItem } from "../types";

type Props = {
  items: ScoreBreakdownItem[];
};

export function BreakdownBars({ items }: Props) {
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
              <span>{Math.round(item.weight * 100)}% веса</span>
            </div>
            <div className="breakdown-bar">
              <div style={{ width: `${item.score}%` }} />
            </div>
            <div className="breakdown-meta">
              <span>{item.summary}</span>
              <strong>{item.score}</strong>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
