import type { PeerRow } from "../types";

type Props = {
  rows: PeerRow[];
  selectedTicker: string;
};

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
                <td>{row.company}</td>
                <td>{row.score}</td>
                <td>${row.market_cap_bln}B</td>
                <td>{row.pe_ratio}</td>
                <td>{row.roe_pct}%</td>
                <td>{row.revenue_growth_pct}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

