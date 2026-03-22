export type MetricCard = {
  label: string;
  value: number;
  unit: string;
  benchmark: number;
  direction: "higher_better" | "lower_better";
  description: string;
};

export type ScoreBreakdownItem = {
  key: string;
  label: string;
  score: number;
  weight: number;
  summary: string;
};

export type PeerRow = {
  ticker: string;
  company: string;
  sector: string;
  industry: string;
  score: number;
  market_cap_bln: number;
  pe_ratio: number;
  roe_pct: number;
  revenue_growth_pct: number;
};

export type FundamentalTrendPoint = {
  period: string;
  revenue_bln: number;
  free_cash_flow_bln: number;
};

export type PriceHistoryPoint = {
  date: string;
  close: number;
};

export type MacroPoint = {
  label: string;
  value: number;
  unit: string;
  source: string;
};

export type AnalysisResponse = {
  ticker: string;
  company: string;
  sector: string;
  industry: string;
  score: number;
  verdict: string;
  narrative: string;
  metric_cards: MetricCard[];
  score_breakdown: ScoreBreakdownItem[];
  peers: PeerRow[];
  fundamentals_history: FundamentalTrendPoint[];
  price_history: PriceHistoryPoint[];
  macro: MacroPoint[];
  assumptions: string[];
  data_sources: string[];
  warnings: string[];
};
