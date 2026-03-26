export type MetricCard = {
  label: string;
  value: number | null;
  unit: string;
  benchmark: number | null;
  direction: "higher_better" | "lower_better";
  display_value?: string | null;
  display_benchmark?: string | null;
  comparison_label?: string | null;
  description: string;
};

export type ScoreBreakdownItem = {
  key: string;
  label: string;
  score: number | null;
  weight: number;
  summary: string;
};

export type PeerRow = {
  ticker: string;
  company: string;
  sector: string;
  industry: string;
  score: number;
  market_cap_bln: number | null;
  market_cap_status: "valid" | "suspect" | "invalid";
  pe_ratio: number | null;
  roe_pct: number | null;
  revenue_growth_pct: number | null;
  quality_class: "usable" | "weak" | "excluded";
  included_in_baseline: boolean;
  baseline_weight?: number | null;
  quality_note?: string | null;
};

export type FundamentalTrendPoint = {
  period: string;
  revenue_bln: number;
  free_cash_flow_bln: number | null;
};

export type PriceHistoryPoint = {
  date: string;
  close: number;
};

export type MacroPoint = {
  label: string;
  value: number | null;
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
