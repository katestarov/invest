import { FormEvent, useEffect, useState } from "react";

import { BreakdownBarsRu } from "./components/BreakdownBarsRu";
import { MacroPanelRu } from "./components/MacroPanelRu";
import { MetricGridRu } from "./components/MetricGridRu";
import { PeerTableRu } from "./components/PeerTableRu";
import { PriceChartRu } from "./components/PriceChartRu";
import { ScoreRingRu } from "./components/ScoreRingRu";
import { TrendChartRu } from "./components/TrendChartRu";
import type { AnalysisResponse } from "./types";

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000/api/v1";
const DEFAULT_TICKER = "AAPL";

export default function AppScreen() {
  const [ticker, setTicker] = useState(DEFAULT_TICKER);
  const [data, setData] = useState<AnalysisResponse | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function loadAnalysis(nextTicker: string) {
    setLoading(true);
    setError("");

    try {
      const response = await fetch(`${API_BASE}/analyze/${nextTicker.toUpperCase()}`);
      if (!response.ok) {
        const payload = (await response.json().catch(() => null)) as { detail?: string } | null;
        throw new Error(payload?.detail ?? "Не удалось получить данные по тикеру");
      }

      const payload = (await response.json()) as AnalysisResponse;
      setData(payload);
      setTicker(nextTicker.toUpperCase());
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Ошибка запроса");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadAnalysis(DEFAULT_TICKER);
  }, []);

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void loadAnalysis(ticker);
  }

  return (
    <main className="app-shell">
      <section className="hero">
        <div className="hero-copy">
          <span className="eyebrow">Investment Intelligence MVP</span>
          <h1>Система оценки инвестиционной привлекательности организаций</h1>
          <p>
            Приложение собирает рыночные, фундаментальные и макроэкономические данные,
            сравнивает компанию с peer-group и выводит итоговую оценку в шкале от 0 до 100.
          </p>
          <form className="ticker-form" onSubmit={handleSubmit}>
            <input
              value={ticker}
              onChange={(event) => setTicker(event.target.value.toUpperCase())}
              placeholder="Введите тикер, например SIBN, AAPL или JPM"
            />
            <button type="submit" disabled={loading}>
              {loading ? "Загрузка..." : "Анализировать"}
            </button>
          </form>
          <div className="quick-tickers">
            {["AAPL", "MSFT", "NVDA", "GOOGL", "JPM", "LLY", "XOM", "TSLA"].map((item) => (
              <button key={item} type="button" onClick={() => void loadAnalysis(item)}>
                {item}
              </button>
            ))}
          </div>
        </div>

        {data && (
          <div className="hero-card">
            <ScoreRingRu score={data.score} verdict={data.verdict} />
            <div className="hero-card-copy">
              <span>{data.ticker}</span>
              <h2>{data.company}</h2>
              <p>{data.narrative}</p>
              <div className="hero-badges">
                <strong>{data.sector}</strong>
                <strong>{data.industry}</strong>
              </div>
            </div>
          </div>
        )}
      </section>

      {error && <section className="error-banner">{error}</section>}

      {data && (
        <>
          <MetricGridRu items={data.metric_cards} />

          <section className="dashboard-grid">
            <BreakdownBarsRu items={data.score_breakdown} />
            <MacroPanelRu items={data.macro} />
          </section>

          <section className="dashboard-grid">
            <TrendChartRu points={data.fundamentals_history} />
            <PriceChartRu points={data.price_history} />
          </section>

          <section className="dashboard-grid">
            <div className="panel notes-panel">
              <div className="panel-head">
                <h3>Прозрачность расчета</h3>
                <span>Допущения и источники</span>
              </div>
              <div className="notes-block">
                <h4>Допущения</h4>
                <ul>
                  {data.assumptions.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
              <div className="notes-block">
                <h4>Источники</h4>
                <ul>
                  {data.data_sources.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
            </div>

            <div className="panel notes-panel">
              <div className="panel-head">
                <h3>Предупреждения по данным</h3>
                <span>Что важно учитывать</span>
              </div>
              <div className="notes-block">
                <ul>
                  {(data.warnings.length ? data.warnings : ["Все источники ответили без явных предупреждений."]).map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
            </div>
          </section>

          <PeerTableRu rows={data.peers} selectedTicker={data.ticker} />
        </>
      )}
    </main>
  );
}
