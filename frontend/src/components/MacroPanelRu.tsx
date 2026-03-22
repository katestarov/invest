import type { MacroPoint } from "../types";

type Props = {
  items: MacroPoint[];
};

export function MacroPanelRu({ items }: Props) {
  return (
    <div className="panel macro-panel">
      <div className="panel-head">
        <h3>Макроэкономический фон</h3>
        <span>Ставки, инфляция и рост экономики</span>
      </div>
      <div className="macro-grid">
        {items.map((item) => (
          <div className="macro-item" key={item.label}>
            <span>{item.label}</span>
            <strong>
              {item.value}
              {item.unit}
            </strong>
            <small>{item.source}</small>
          </div>
        ))}
      </div>
    </div>
  );
}

