# Score Validation Summary

Этот отчёт не пытается доказать инвестиционную истинность модели. Он проверяет устойчивость, интерпретируемость и методическую непротиворечивость score на контролируемых сценариях.

## Baseline scenarios
- `strong_non_bank`: score `59.2`, verdict `Нейтрально (Neutral)`, valuation mode `normal`, baseline `peer`
- `regular_non_bank`: score `57.2`, verdict `Нейтрально (Neutral)`, valuation mode `low_confidence`, baseline `peer`
- `bank_like`: score `38.1`, verdict `Повышенный риск (High Caution)`, valuation mode `normal`, baseline `peer`
- `incomplete_data`: score `23.7`, verdict `Повышенный риск (High Caution)`, valuation mode `normal`, baseline `peer`
- `fallback_baseline`: score `32.8`, verdict `Повышенный риск (High Caution)`, valuation mode `fallback_low_confidence`, baseline `peer`

## Key findings
- Средняя чувствительность к изменению весов остаётся ограниченной; наиболее чувствительный блок: `valuation` с mean abs delta `0.86`.
- Наиболее заметная реакция на изменение входа пришлась на `revenue_current_plus_10pct` с abs delta `4.0`.
- При missingness mean abs delta составила `3.6`, а доля неизменного verdict — `1.0`.
- Наибольшая перестановка рангов возникает в `profitability_0.8x`, но даже там mean abs rank delta равна `0`.

## Interpretation
- Score меняется в ожидаемую сторону при осмысленных perturbation-сценариях.
- Coverage и renormalization сглаживают деградацию при удалении части метрик вместо резкого обвала всей модели.
- Peer baseline показывает предсказуемый переход от `normal` к `low_confidence` и `fallback`, а valuation weight уменьшается вместе с качеством peer support.