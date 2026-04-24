# Computation Core Coverage

## Покрытые функции и расчеты

- `safe_ratio`
- `premium_pct`
- `score_positive`
- `score_inverse`
- `score_relative_valuation`
- `coverage_ratio`
- `normalize_weights`
- `apply_low_confidence_cap`
- `AnalysisService._market_cap_diagnostics`
- `AnalysisService._pe_ratio`
- `AnalysisService._pb_ratio`
- `AnalysisService._roe_pct`
- `AnalysisService._revenue_growth_pct`
- `AnalysisService._revenue_cagr_like_pct`
- `AnalysisService._roe_assessment`
- `AnalysisService._build_weighted_scores` для `bank-like`
- `YahooFinanceProvider.fetch_company_bundle` для `1Y Return` и `5Y Return`
- `SecEdgarProvider.fetch_company_bundle` для `ROIC`, `EBIT Margin`, `FCF Margin`, `Debt/Equity`, `Current Ratio`
- `FredProvider.fetch_macro_bundle` для year-over-year inflation

## Граничные случаи

- `None` во входах
- нулевой знаменатель
- отрицательный знаменатель
- слишком маленькая equity base для надежного `ROE`
- неполные tax facts и estimated tax rate для `ROIC`
- несопоставимые периоды выручки для `Revenue Growth`
- сильный разброс market cap между источниками
- out-of-range market cap
- partial / low-confidence scoring branches
- bank-like и non-bank ветвление
