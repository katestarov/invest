import type { PeerRow } from "../types";

type Props = {
  rows: PeerRow[];
  selectedTicker: string;
};

function displayValue(value: number | null, suffix = "") {
  return value === null ? "N/A" : `${value}${suffix}`;
}

function qualityLabel(row: PeerRow) {
  if (row.quality_note) {
    return row.quality_note;
  }
  if (row.quality_class === "weak") {
    return "Limited data";
  }
  if (row.quality_class === "excluded") {
    return "Excluded from baseline";
  }
  if (row.market_cap_status === "suspect") {
    return "Suspect market cap";
  }
  return null;
}

export function PeerTable({ rows, selectedTicker }: Props) {
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
                  {qualityLabel(row) ? <small>{qualityLabel(row)}</small> : null}
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
