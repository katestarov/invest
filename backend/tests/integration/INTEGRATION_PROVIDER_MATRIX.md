# Provider Integration Matrix

| Источник | Успешный кейс | Ошибка | Ожидаемое поведение |
| --- | --- | --- | --- |
| Yahoo Finance | `chart + quote` корректно парсятся в price history, returns, market cap snapshot | пустой `chart.result` | Явная ошибка `ValueError`, потому что для chart fallback не предусмотрен |
| Yahoo Finance | `chart` доступен, `quote` недоступен | `401/timeout/5xx` на optional quote snapshot | Анализ не падает: используется chart-only fallback, quote-поля остаются `None` |
| SEC EDGAR | facts/submissions дают revenue, ROIC, margins, shares | пустые facts | Возвращается безопасный payload с `None`/пустыми списками и warnings |
| SEC EDGAR | частично заполненные facts | отсутствуют tax facts / FCF alignment | Возвращаются частичные метрики и предупреждения, где fallback допустим |
| FRED | есть наблюдения по всем сериям | нет API key | Возвращается пустой macro payload и warning, без падения анализа |
| FRED | частично пустые observations | пустой series payload | Значения становятся `None`, macro block потом деградирует безопасно |
| World Bank | есть список значений | пустой ответ | `gdp_growth_pct=None`, без аварии |
| FMP peers | peers API отдаёт tickers | `429` | Возвращается пустой `PeerDiscoveryResult`, анализ идёт дальше |
| FMP/Finnhub market cap | snapshot доступен | supplemental source timeout | Источник пропускается, весь анализ не падает |
| Finnhub peers | peers API отдаёт tickers | `timeout/5xx` | Ошибка пробрасывается на уровень orchestration, где отдельный provider может быть пропущен |
| BusinessType / Config | локальные fallback rules | нет live peer APIs | Возвращается локальный безопасный peer universe |
| Base HTTP provider | успешный ответ | повторный такой же вызов | Берётся cache path, число сетевых вызовов не растёт |
| Base HTTP provider | временная ошибка | `timeout/5xx` | Происходит retry до `PROVIDER_RETRY_ATTEMPTS` |
| Base HTTP provider | rate limit | `429` | Retry loop останавливается сразу |
