import type { PeerRow } from "../types";

type Props = {
  rows: PeerRow[];
  selectedTicker: string;
};

function displayValue(value: number | null, suffix = "") {
  return value === null ? "N/A" : `${value}${suffix}`;
}

function statusLabels(row: PeerRow) {
  const labels: string[] = [];
  if (row.quality_note) {
    labels.push(row.quality_note);
  } else if (row.quality_class === "weak") {
    labels.push("Limited data");
  } else if (row.quality_class === "excluded") {
    labels.push("Excluded from baseline");
  }
  if (row.market_cap_status === "suspect") {
    labels.push("Suspect market cap");
  }
  if (row.included_in_baseline) {
    if ((row.baseline_weight ?? 0) < 0.999) {
      labels.push(`Included with reduced weight (${displayValue(row.baseline_weight ?? null)})`);
    } else {
      labels.push("Included in baseline");
    }
  } else if (row.quality_class !== "excluded") {
    labels.push("Not used in current baseline");
  }
  return [...new Set(labels)];
}

export function PeerTableRu({ rows, selectedTicker }: Props) {
  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Сравнение с peer-group</h3>
        <span>Компании того же сектора</span>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Тикер</th>
              <th>Компания</th>
              <th>Оценка</th>
              <th>Капитализация</th>
              <th>P/E</th>
              <th>ROE</th>
              <th>Рост выручки</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.ticker} className={row.ticker === selectedTicker ? "selected-row" : ""}>
                <td>{row.ticker}</td>
                <td>
                  <div>{row.company}</div>
                  {statusLabels(row).length > 0 ? <small>{statusLabels(row).join(" • ")}</small> : null}
                </td>
                <td>{row.score}</td>
                <td>{displayValue(row.market_cap_bln, "B") === "N/A" ? "N/A" : `$${displayValue(row.market_cap_bln, "B")}`}</td>
                <td>{displayValue(row.pe_ratio)}</td>
                <td>{displayValue(row.roe_pct, "%")}</td>
                <td>{displayValue(row.revenue_growth_pct, "%")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
