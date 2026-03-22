import { FormEvent, useEffect, useState } from "react";

import { BreakdownBars } from "./components/BreakdownBars";
import { MacroPanel } from "./components/MacroPanel";
import { MetricGrid } from "./components/MetricGrid";
import { PeerTable } from "./components/PeerTable";
import { ScoreRing } from "./components/ScoreRing";
import { TrendChart } from "./components/TrendChart";
import type { AnalysisResponse } from "./types";

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000/api/v1";
const DEFAULT_TICKER = "AAPL";

export default function App() {
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
        throw new Error("Ticker not found in the demo dataset");
      }

      const payload = (await response.json()) as AnalysisResponse;
      setData(payload);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Request failed");
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
            FastAPI + React приложение, которое агрегирует несколько источников,
            нормализует показатели и показывает итоговый скор компании внутри её сектора.
          </p>
          <form className="ticker-form" onSubmit={handleSubmit}>
            <input
              value={ticker}
              onChange={(event) => setTicker(event.target.value)}
              placeholder="Введите тикер, например AAPL"
            />
            <button type="submit" disabled={loading}>
              {loading ? "Загрузка..." : "Анализировать"}
            </button>
          </form>
          <div className="quick-tickers">
            {["AAPL", "MSFT", "NVDA", "AMZN", "TSLA", "JPM"].map((item) => (
              <button key={item} type="button" onClick={() => void loadAnalysis(item)}>
                {item}
              </button>
            ))}
          </div>
        </div>

        {data && (
          <div className="hero-card">
            <ScoreRing score={data.score} verdict={data.verdict} />
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
          <MetricGrid items={data.metric_cards} />

          <section className="dashboard-grid">
            <BreakdownBars items={data.score_breakdown} />
            <MacroPanel items={data.macro} />
          </section>

          <section className="dashboard-grid">
            <TrendChart points={data.trends} />
            <div className="panel notes-panel">
              <div className="panel-head">
                <h3>Assumptions & Sources</h3>
                <span>MVP transparency layer</span>
              </div>
              <div className="notes-block">
                <h4>Assumptions</h4>
                <ul>
                  {data.assumptions.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
              <div className="notes-block">
                <h4>Sources</h4>
                <ul>
                  {data.data_sources.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
            </div>
          </section>

          <PeerTable rows={data.peers} selectedTicker={data.ticker} />
        </>
      )}
    </main>
  );
}

