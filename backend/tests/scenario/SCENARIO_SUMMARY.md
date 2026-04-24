# Scenario Test Summary

## Strong non-bank company

Моделируется сильная software-компания с хорошими фундаментальными данными, тремя usable peers, одним weak peer и нормальным valuation baseline без деградации.

## Incomplete-data company

Моделируется компания с неполными фактами SEC и частично отсутствующими метриками, где система должна сохранить score, warnings и объяснимый UI payload без аварии.

## Bank-like company

Моделируется bank-like компания, у которой профиль, peer universe и scoring должны идти по банковской ветке, а не по generic non-bank формулам.

## Fallback baseline under peer degradation

Моделируется peer-group с деградацией качества: один usable peer, несколько weak peers и excluded строка. Система должна включить reduced-weight valuation fallback, но не позволить weak peers доминировать baseline.
