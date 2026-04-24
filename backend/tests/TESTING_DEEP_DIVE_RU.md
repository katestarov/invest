# Подробное руководство по тестовой системе backend

## 1. Зачем вообще нужна эта тестовая система

Тестовая система в этом проекте нужна не просто для проверки того, что код “не падает”. Ее цель шире:

- проверять корректность вычислительного ядра инвестиционного скоринга;
- проверять, что интеграция с внешними источниками данных не ломает анализ;
- проверять, что peer pipeline, baseline, fallback-режимы и warning-логика ведут себя предсказуемо;
- позволять воспроизводимо прогонять анализ без реального интернета и без обязательных реальных API-ключей;
- давать численные и сценарные материалы для ВКР, технической документации и защиты архитектурных решений.

Именно поэтому тесты разделены на несколько уровней. Каждый уровень отвечает на свой вопрос:

- `unit` отвечает на вопрос “правильно ли считается конкретная формула или нормализация?”
- `integration` отвечает на вопрос “правильно ли ведет себя провайдер или API wiring?”
- `scenario` отвечает на вопрос “что получится, если прогнать целую бизнес-ситуацию от тикера до финального score?”
- `validation` отвечает на вопрос “устойчив ли score как метод и как система?”

Важный принцип: почти весь тестовый контур построен так, чтобы работать на mock-данных и fake providers. Это означает, что тесты не должны зависеть от внешнего состояния Yahoo, SEC, FRED, World Bank, FMP, Finnhub, скорости интернета, rate limit или наличия live API в момент запуска.

---

## 2. Что именно входит в тестовую систему

### 2.1 Основная структура

Тестовая система backend состоит из следующих частей:

- [conftest.py](/D:/Downloads/Dev/invest/backend/tests/conftest.py)
- [README.md](/D:/Downloads/Dev/invest/backend/tests/README.md)
- [test_scoring_safety.py](/D:/Downloads/Dev/invest/backend/tests/test_scoring_safety.py)
- [unit/test_computation_core.py](/D:/Downloads/Dev/invest/backend/tests/unit/test_computation_core.py)
- [unit/test_logging_noise.py](/D:/Downloads/Dev/invest/backend/tests/unit/test_logging_noise.py)
- [integration/test_api_analysis_endpoint.py](/D:/Downloads/Dev/invest/backend/tests/integration/test_api_analysis_endpoint.py)
- [integration/test_provider_integration.py](/D:/Downloads/Dev/invest/backend/tests/integration/test_provider_integration.py)
- [scenario/test_analysis_scenarios.py](/D:/Downloads/Dev/invest/backend/tests/scenario/test_analysis_scenarios.py)
- [scenario/test_business_logic_scenarios.py](/D:/Downloads/Dev/invest/backend/tests/scenario/test_business_logic_scenarios.py)
- [support/fakes.py](/D:/Downloads/Dev/invest/backend/tests/support/fakes.py)
- [support/factories.py](/D:/Downloads/Dev/invest/backend/tests/support/factories.py)
- [support/raw_payloads.py](/D:/Downloads/Dev/invest/backend/tests/support/raw_payloads.py)

Дополнительно есть исследовательский слой валидации, который не является обычным `pytest`-тестом, но использует ту же тестовую инфраструктуру:

- [app/validation/common.py](/D:/Downloads/Dev/invest/backend/app/validation/common.py)
- [app/validation/score_validation.py](/D:/Downloads/Dev/invest/backend/app/validation/score_validation.py)
- [app/validation/benchmark_diagnostics.py](/D:/Downloads/Dev/invest/backend/app/validation/benchmark_diagnostics.py)
- [app/validation/README.md](/D:/Downloads/Dev/invest/backend/app/validation/README.md)

---

## 3. Как запускать тесты

### 3.1 Одна команда для полного прогона

Из корня репозитория:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests -q
```

Это основной полный прогон. Он запускает:

- unit-тесты;
- integration-тесты;
- scenario-тесты;
- regression pack из `test_scoring_safety.py`.

### 3.2 Частичные прогоны

Только unit:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests/unit -q
```

Только integration:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests/integration -q
```

Только scenario:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests/scenario -q
```

Только regression safety pack:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests/test_scoring_safety.py -q
```

### 3.3 Запуск validation-скриптов

Из папки `backend/`:

```powershell
.\.venv\Scripts\python.exe -m app.validation.score_validation
.\.venv\Scripts\python.exe -m app.validation.benchmark_diagnostics
```

Они не заменяют `pytest`, а дополняют его. Их задача не проверить “pass/fail”, а сгенерировать таблицы, графики и исследовательские выводы.

---

## 4. Главный принцип архитектуры тестов

### 4.1 Мы тестируем реальную бизнес-логику, но на подмененных зависимостях

Ключевой объект анализа в системе — это `AnalysisService` из runtime path. Идея тестовой архитектуры такая:

- не переписывать весь runtime code под тесты;
- не дублировать бизнес-логику внутри тестов;
- не эмулировать поведение системы “вручную” там, где можно прогнать реальные методы сервиса;
- вместо этого подменять внешние источники данных и persistence.

То есть тесты стараются запускать:

- реальные методы провайдеров;
- реальные методы `AnalysisService`;
- реальный FastAPI endpoint;
- реальные ветки логики peer selection, market cap normalization, scoring, warnings, baseline modes;

но при этом подставляют:

- fake Yahoo/SEC/FRED/World Bank payloads;
- fake peer providers;
- fake persistence layer;
- dependency overrides для FastAPI.

Так достигается хороший баланс:

- код под тестом остается близок к production runtime;
- тесты воспроизводимы;
- тесты быстрые;
- тесты не зависят от нестабильных внешних API.

---

## 5. Роль `conftest.py`

Файл [conftest.py](/D:/Downloads/Dev/invest/backend/tests/conftest.py) — это точка сборки всей тестовой инфраструктуры.

### 5.1 Что он делает при инициализации

В начале файла он:

- добавляет `backend` в `sys.path`, чтобы модули импортировались стабильно;
- выставляет тестовые значения окружения, например:
  - `DATABASE_URL=sqlite+pysqlite:///:memory:`
  - `APP_ENV=test`
- заранее импортирует некоторые реальные модули, чтобы старые unittest-style тесты не сломали окружение через `sys.modules.setdefault`.

Это важно, потому что в проекте есть legacy-части тестов, и `conftest.py` обеспечивает единое стабильное тестовое окружение.

### 5.2 Какие базовые фикстуры там есть

В `conftest.py` определены фикстуры нескольких уровней.

#### Базовые payload-фикстуры

Они нужны для интеграционных тестов провайдеров:

- `yahoo_chart_payload`
- `yahoo_quote_payload`
- `sec_facts_payload`
- `sec_submissions_payload`
- `fred_payloads`
- `world_bank_payload`
- `fmp_peer_payload`
- `finnhub_peer_payload`

Эти фикстуры дают уже собранные mock payloads, похожие на реальные ответы внешних API.

#### Dataset-фикстуры

Они нужны для scenario-слоя:

- `regular_company_dataset`
- `strong_company_dataset`
- `bank_company_dataset`
- `incomplete_company_dataset`
- `fallback_baseline_dataset`
- `peer_group_with_quality_classes`

Каждый dataset — это не один payload, а целая модель мира для теста:

- основной тикер;
- Yahoo-данные компании и peers;
- SEC-данные компании и peers;
- макроданные;
- peer discovery map;
- supplemental market cap snapshots.

#### Service-фикстуры

Это уже готовые экземпляры `AnalysisService`, собранные поверх fake dependencies:

- `regular_analysis_service`
- `strong_analysis_service`
- `bank_analysis_service`
- `incomplete_analysis_service`
- `fallback_analysis_service`

Каждая такая фикстура подготавливает сервис так, будто он работает в production, но вместо реальных сетевых провайдеров получает fake implementations.

#### API-фикстура

Фикстура `api_client_factory` строит `FastAPI TestClient` и подменяет зависимость:

- `app.dependency_overrides[routes.get_analysis_service]`

Это позволяет тестировать endpoint `/analyze/{ticker}` не через live runtime, а через заранее собранный тестовый `AnalysisService`.

---

## 6. Как работают fake providers

Основной набор fake-объектов лежит в [support/fakes.py](/D:/Downloads/Dev/invest/backend/tests/support/fakes.py).

### 6.1 `StaticCompanyBundleProvider`

Назначение:

- подменяет Yahoo или SEC провайдер;
- на вход получает словарь `ticker -> payload`;
- на вызов `fetch_company_bundle(ticker)` возвращает `ProviderResult(payload=..., warnings=...)`.

Что важно:

- сохраняет список вызовов в `calls`;
- возвращает глубокую копию payload, чтобы тесты не портили исходные данные между вызовами.

Это позволяет:

- считать количество обращений;
- проверять cache behavior;
- запускать реальные методы `AnalysisService` без live HTTP.

### 6.2 `StaticMacroProvider`

Назначение:

- подменяет FRED или World Bank;
- выдает один заранее заданный макро-bundle;
- считает число вызовов через `calls`.

### 6.3 `StaticPeerProvider`

Назначение:

- подменяет peer provider;
- умеет возвращать `PeerDiscoveryResult`;
- умеет отдавать supplemental `market_cap_snapshot`.

Он строится из:

- `source_name`
- `discovery_map`
- `reason_map`
- `market_cap_snapshots`

Эта подмена особенно важна для peer pipeline, потому что именно она позволяет воспроизводимо моделировать:

- нормальный peer universe;
- пустой peer universe;
- fallback universe;
- weak/excluded peers;
- suspect market cap;
- multi-source market cap disagreement.

### 6.4 `RecordingPersistence`

Состоит из:

- `RecordingSession`
- `RecordingRepository`
- `RecordingPersistence`

Назначение:

- заменить реальную БД в тестах;
- сохранять факт записи bronze/silver/gold уровней;
- не требовать PostgreSQL и реального транзакционного контура.

Это особенно полезно для интеграционного и scenario-слоя, где важно не только вернуть response, но и не уронить runtime path, в котором присутствует persistence.

---

## 7. Как строятся synthetic datasets

Файл [support/factories.py](/D:/Downloads/Dev/invest/backend/tests/support/factories.py) — это фабрика тестовых миров.

### 7.1 Почему datasets важнее одиночных payloads

Для аналитической системы одного mock-ответа Yahoo недостаточно. Чтобы протестировать scoring, нужны одновременно:

- рыночные данные;
- фундаментальные данные;
- макроданные;
- peer discovery;
- peer market caps;
- разные типы peers.

Именно поэтому `factories.py` строит datasets целиком.

### 7.2 Базовые builder-функции

Верхний уровень:

- `make_yahoo_payload(...)`
- `make_edgar_payload(...)`

Они собирают унифицированные словари, которые понимает runtime service.

`make_yahoo_payload(...)` обычно включает:

- `ticker`
- `company`
- `currency`
- `current_price`
- `market_cap_bln_quote`
- `shares_outstanding_quote_mln`
- `one_year_return_pct`
- `five_year_return_pct`
- `price_history`

`make_edgar_payload(...)` обычно включает:

- `company`
- `sector`
- `industry`
- `sic`
- `revenue_bln`
- `free_cash_flow_bln`
- `current_ratio`
- `debt_to_equity`
- `roic_pct`
- `ebit_margin_pct`
- `fcf_margin_pct`
- `net_income_bln`
- `shares_outstanding_mln`
- `assets_bln`
- `equity_bln`
- `history`
- `revenue_periods`

### 7.3 Основные готовые datasets

#### `make_regular_company_dataset()`

Моделирует нормальную небанковскую software-компанию с peer set:

- usable peers;
- один weak peer;
- один excluded peer.

Это базовый “здоровый” мир для большинства сценарных тестов.

#### `make_strong_company_dataset()`

Это расширение regular dataset, где добавлен еще один сильный usable peer. Нужен для сценария “сильная компания + normal baseline”.

#### `make_bank_company_dataset()`

Моделирует bank-like ветку с банками в peer universe. Нужен, чтобы проверить:

- определение `BANK`;
- другую scoring-логику;
- bank-specific peers.

#### `make_incomplete_company_dataset()`

Моделирует компанию с неполными фундаментальными данными и несопоставимыми/частично пустыми значениями.

Нужен, чтобы проверить:

- работу coverage;
- renormalization;
- консервативную деградацию score;
- отсутствие краша при дырявых данных.

#### `make_fallback_baseline_dataset()`

Один из самых важных datasets. Он моделирует деградацию peer-group:

- `1 usable`
- `2 weak`
- `1 excluded`

Именно на нем проверяется:

- fallback valuation;
- reduced valuation weight;
- suspect market cap;
- недоминирование weak peers.

---

## 8. Как собирается тестовый `AnalysisService`

В `conftest.py` есть фикстура `analysis_service_factory`, а в validation-слое похожую роль играет [build_mock_analysis_service()](/D:/Downloads/Dev/invest/backend/app/validation/common.py#L140).

Идея одна и та же:

- взять synthetic datasets;
- собрать из них fake providers;
- передать их в настоящий `AnalysisService`.

### 8.1 Что именно инжектится

В сервис подаются:

- `yahoo=StaticCompanyBundleProvider(...)`
- `edgar=StaticCompanyBundleProvider(...)`
- `fred=StaticMacroProvider(...)`
- `world_bank=StaticMacroProvider(...)`
- `peer_providers=[...]`
- `session_factory=...`
- `repository_factory=...`

Таким образом, тесты не подделывают сам сервис. Они просто дают ему контролируемые зависимости.

### 8.2 Что это даёт на практике

Можно воспроизводимо тестировать:

- `_resolve_company_profile(...)`
- `_build_peer_group(...)`
- `_build_peer_averages(...)`
- `_build_silver_metrics(...)`
- `_build_weighted_scores(...)`
- `analyze(...)`

То есть тесты реально проходят через бизнес-логику, которая используется в runtime.

---

## 9. Unit-тесты: что именно они проверяют

Основной файл: [unit/test_computation_core.py](/D:/Downloads/Dev/invest/backend/tests/unit/test_computation_core.py)  
Отчет: [unit/COMPUTATION_CORE_REPORT.md](/D:/Downloads/Dev/invest/backend/tests/unit/COMPUTATION_CORE_REPORT.md)

### 9.1 Идея unit-слоя

Unit-тесты проверяют небольшие участки вычислительной логики изолированно:

- формулы;
- нормализации;
- защиту от плохих входных данных;
- bank vs non-bank ветвление;
- edge cases.

Здесь важен принцип “понятное expected value”.  
То есть каждый тест должен отвечать на вопрос:

- что пришло на вход;
- почему именно такой результат ожидается;
- какая ветка логики проверяется.

### 9.2 Что покрыто

Покрыты:

- `safe_ratio`
- market cap diagnostics / normalization
- `P/E`
- `P/B`
- `ROE`
- `ROIC`
- `EBIT Margin`
- `FCF Margin`
- `Debt/Equity`
- `Current Ratio`
- `Revenue Growth`
- `Revenue CAGR-like`
- `1Y Return`
- `5Y Return`
- `score_positive`
- `score_inverse`
- `premium_pct`
- `score_relative_valuation`
- `coverage_ratio`
- перераспределение весов
- low-confidence cap

### 9.3 Какие edge cases там особенно важны

Проверяются сценарии:

- `None`
- пустые значения
- деление на `0`
- отрицательный знаменатель
- unreliable ROE
- incomplete tax facts
- несопоставимые периоды revenue growth
- неполные наборы фундаментальных данных
- bank-like и non-bank ветки

Unit-тесты отвечают за математическую честность модели. Если здесь ошибка, то вся модель может “логично выглядеть”, но считать неправильно.

---

## 10. Unit-тесты логирования

Файл: [unit/test_logging_noise.py](/D:/Downloads/Dev/invest/backend/tests/unit/test_logging_noise.py)

Его задача более утилитарная:

- следить, чтобы система не зашумляла консоль служебными HTTP-сообщениями;
- контролировать, что optional Yahoo snapshot failure не превращается в бесполезный warning-спам.

Это не про финансы, а про операционное качество backend.

---

## 11. Интеграционные тесты: как они устроены

Основной файл: [integration/test_provider_integration.py](/D:/Downloads/Dev/invest/backend/tests/integration/test_provider_integration.py)  
Матрица сценариев: [integration/INTEGRATION_PROVIDER_MATRIX.md](/D:/Downloads/Dev/invest/backend/tests/integration/INTEGRATION_PROVIDER_MATRIX.md)

### 11.1 Идея integration-слоя

Integration-тесты проверяют, как отдельные крупные узлы системы взаимодействуют между собой:

- провайдер и его HTTP/cache/retry поведение;
- FastAPI и dependency injection;
- `AnalysisService` и набор peer providers;
- cache path;
- degraded mode.

Они крупнее unit-тестов, но все еще работают в контролируемом mock-окружении.

### 11.2 Вспомогательные классы внутри provider integration tests

Внутри `test_provider_integration.py` есть служебные тестовые объекты:

- `_JsonResponse`
- `_SequenceClient`
- `_http_status_error(...)`

Они нужны, чтобы программно строить цепочки ответов:

- success;
- timeout;
- `429`;
- `502`;
- пустой payload;
- partial payload.

Это позволяет проверять retry и fallback детерминированно.

### 11.3 Какие провайдеры покрываются

Покрываются:

- `BaseHttpProvider`
- `YahooFinanceProvider`
- `SecEdgarProvider`
- `FredProvider`
- `WorldBankProvider`
- `FmpPeerProvider`
- `FinnhubPeerProvider`
- `BusinessTypePeerProvider`
- `ConfigPeerProvider`

### 11.4 Что именно проверяется у провайдеров

#### `BaseHttpProvider`

Проверяется:

- cache reuse;
- retry count;
- stop-on-429 behavior.

Это фундаментально важно, потому что все HTTP-провайдеры наследуют или используют этот базовый контур.

#### `YahooFinanceProvider`

Проверяется:

- корректный success path;
- частичный `quote` fallback;
- понятная ошибка при пустом `chart` payload.

То есть Yahoo path тестируется как на “хорошую” структуру, так и на реалистичные degraded payloads.

#### `SecEdgarProvider`

Проверяется:

- пустой facts payload;
- частично заполненный facts payload;
- восстановление метрик при неполной tax-информации;
- warnings для оценочных расчетов.

#### `FredProvider`

Проверяется:

- отсутствие API key;
- пустые observations.

#### `WorldBankProvider`

Проверяется:

- успешный parsing;
- пустой payload.

#### Peer providers

Проверяется:

- success;
- empty;
- `429`;
- timeout;
- `5xx`;
- safe fallback в business/config providers.

### 11.5 Проверки на отказоустойчивость

Особенно важны тесты:

- `test_analysis_survives_peer_provider_discovery_failure(...)`
- `test_analysis_survives_supplemental_market_cap_source_failure(...)`
- `test_analysis_cache_reuses_previous_result(...)`

Они уже не просто про отдельный provider, а про устойчивость целой аналитической системы.

Эти тесты доказывают:

- падение одного peer provider не валит анализ;
- падение одного supplemental market cap source не валит анализ;
- повторный вызов действительно использует cache и не дергает провайдеры повторно.

---

## 12. Интеграционные тесты API

Файл: [integration/test_api_analysis_endpoint.py](/D:/Downloads/Dev/invest/backend/tests/integration/test_api_analysis_endpoint.py)

Этот слой проверяет уже не просто методы Python-классов, а реальный API wiring:

- dependency override;
- `FastAPI TestClient`;
- корректный контракт ответа `/analyze/{ticker}`;
- отсутствие необходимости в live runtime dependencies.

Принцип такой:

1. Собирается тестовый `AnalysisService`.
2. Он подставляется в FastAPI через `app.dependency_overrides`.
3. Делается HTTP-вызов через `TestClient`.
4. Проверяется уже API payload.

Это важно, потому что позволяет ловить ошибки контрактов, схем и сериализации отдельно от чистой вычислительной логики.

---

## 13. Scenario-тесты: что они моделируют

Основной файл: [scenario/test_business_logic_scenarios.py](/D:/Downloads/Dev/invest/backend/tests/scenario/test_business_logic_scenarios.py)  
Legacy scenario pack: [scenario/test_analysis_scenarios.py](/D:/Downloads/Dev/invest/backend/tests/scenario/test_analysis_scenarios.py)  
Сводка: [scenario/SCENARIO_SUMMARY.md](/D:/Downloads/Dev/invest/backend/tests/scenario/SCENARIO_SUMMARY.md)

### 13.1 Зачем нужны scenario-тесты

Если unit-тесты отвечают за формулы, а integration — за узлы системы, то scenario-тесты отвечают за бизнес-правду:

- какую картину мира видит система;
- как она определяет тип бизнеса;
- как собирает peers;
- как классифицирует peers;
- как строит baseline;
- как деградирует valuation;
- что в итоге попадает в UI response.

### 13.2 Сценарий 1: сильная non-bank компания

Проверяется:

- software-profile;
- минимум 3 usable peers;
- `valuation_support_mode = normal`;
- ненулевой valuation block;
- корректные `quality_class` по peers:
  - usable
  - weak
  - excluded

Этот сценарий проверяет “здоровый режим”.

### 13.3 Сценарий 2: неполные данные

Проверяется:

- профиль компании;
- наличие warnings;
- data completeness < 100;
- часть карточек метрик пустая;
- score всё равно строится;
- peers не исчезают полностью.

Этот сценарий проверяет, что модель умеет деградировать аккуратно, а не просто падать.

### 13.4 Сценарий 3: bank-like компания

Проверяется:

- `business_type = BANK`;
- `is_bank_like = True`;
- peer universe из банков;
- bank-specific scoring branch;
- baseline строится через банковскую ветку.

Этот сценарий нужен потому, что bank-like компании нельзя честно оценивать теми же правилами, что software или industrials.

### 13.5 Сценарий 4: fallback peer-group

Это один из самых методически важных сценариев.

Проверяется:

- `1 usable + 2 weak`;
- reduced valuation weight;
- fallback mode вместо полного disable;
- weak peers не доминируют baseline;
- `GM` usable;
- `F` и `RIVN` weak;
- `LCID` excluded;
- warnings прямо говорят о fallback.

Этот сценарий доказывает, что система умеет не только работать в идеальных условиях, но и правдоподобно деградировать.

---

## 14. Роль `test_scoring_safety.py`

Файл: [test_scoring_safety.py](/D:/Downloads/Dev/invest/backend/tests/test_scoring_safety.py)

Это большой regression/safety pack, который исторически покрывает много важных участков модели:

- scoring safety;
- valuation gating;
- peer quality;
- fallback behavior;
- market cap outliers;
- bank/non-bank safety;
- guardrails от нулевых и шумных значений.

Он не заменяет новые `unit/integration/scenario` папки, а дополняет их.  
Если говорить инженерно, это большой регрессионный сет, который защищает систему от “тихих” поломок при последующих правках.

Когда меняется peer logic, valuation gating, weight renormalization или scoring thresholds, именно этот файл чаще всего ловит неожиданные побочные эффекты.

---

## 15. Как validation-модули используют ту же тестовую инфраструктуру

Validation-слой лежит в [app/validation](/D:/Downloads/Dev/invest/backend/app/validation).

### 15.1 `common.py`

Файл [common.py](/D:/Downloads/Dev/invest/backend/app/validation/common.py) — это мост между pytest-инфраструктурой и исследовательскими скриптами.

Он:

- импортирует те же dataset factories;
- импортирует те же fake providers;
- умеет строить mock `AnalysisService`;
- умеет делать снимок анализа через `collect_analysis_snapshot(...)`;
- умеет записывать `CSV`, `JSON`, `SVG`.

То есть validation-скрипты не изобретают отдельный мир. Они используют ту же самую модель тестовых данных.

### 15.2 `score_validation.py`

Файл [score_validation.py](/D:/Downloads/Dev/invest/backend/app/validation/score_validation.py) отвечает за методическую устойчивость score.

Он делает:

- sensitivity analysis по весам блоков;
- sensitivity analysis по входным данным;
- robustness to missingness;
- peer baseline robustness;
- ranking stability.

На выходе он генерирует:

- `baseline_outcomes.csv`
- `weight_sensitivity.csv`
- `input_sensitivity.csv`
- `missingness_robustness.csv`
- `peer_baseline_robustness.csv`
- `ranking_stability.csv`
- `validation_summary.json`
- несколько `SVG` графиков
- `summary.md`

Результат попадает в [validation_outputs/score_validation](/D:/Downloads/Dev/invest/backend/validation_outputs/score_validation).

### 15.3 `benchmark_diagnostics.py`

Файл [benchmark_diagnostics.py](/D:/Downloads/Dev/invest/backend/app/validation/benchmark_diagnostics.py) отвечает за нефункциональные характеристики:

- synthetic response time;
- cache effect;
- degraded mode timing;
- retry probe;
- timeout snapshot;
- cache TTL / cache keys / fallback modes.

Он генерирует:

- `benchmark_scenarios.csv`
- `system_metrics_table.csv`
- `benchmark_summary.json`
- `response_times.svg`
- `cache_benefit.svg`
- `summary.md`

Результат попадает в [validation_outputs/benchmark_diagnostics](/D:/Downloads/Dev/invest/backend/validation_outputs/benchmark_diagnostics).

---

## 16. Как именно тестируется peer pipeline

Peer pipeline — одна из самых сложных частей системы, и тесты специально построены так, чтобы проверять не только финальный список peers, но и внутренние режимы.

### 16.1 Что считается в peer pipeline

На уровне тестов нас интересуют:

- peer candidate discovery;
- union нескольких peer sources;
- deduplication;
- `usable / weak / excluded`;
- market cap diagnostics;
- `valid / suspect / invalid`;
- baseline inclusion;
- baseline weight;
- fallback modes:
  - `normal`
  - `low_confidence`
  - `fallback_low_confidence`
  - `weak_only_fallback`
  - `disabled`

### 16.2 Как это проверяется

Часть логики проверяется unit/regression тестами, но самые важные свойства проверяются в scenario pack:

- наличие usable peers;
- наличие weak peers;
- reduced valuation weight;
- факт включения в baseline;
- недоминирование weak peers;
- корректные warning messages;
- корректные `quality_note` для UI.

То есть peer pipeline проверяется одновременно:

- как алгоритм;
- как часть scoring;
- как часть UI payload.

---

## 17. Как тестируется cache

### 17.1 Provider cache

Проверяется на `BaseHttpProvider`:

- одинаковый URL и params не должны вызывать второй реальный HTTP-call;
- число вызовов fake client должно остаться `1`.

### 17.2 Analysis cache

Проверяется на `AnalysisService`:

- первый `analyze("ACME")` делает реальную работу через fake providers;
- второй `analyze("ACME")` должен вернуть эквивалентный response без повторных вызовов Yahoo/EDGAR providers.

Это важно и для производительности, и для корректной интерпретации нефункциональных метрик.

---

## 18. Как тестируются retry и timeout

### 18.1 Где хранятся настройки

В [settings.py](/D:/Downloads/Dev/invest/backend/app/core/settings.py):

- `request_timeout_seconds`
- `provider_retry_attempts`
- `analysis_cache_ttl_seconds`
- `provider_cache_ttl_seconds`

### 18.2 Как проверяется retry

Integration-тесты подсовывают `BaseHttpProvider` последовательность ответов:

1. timeout
2. `502`
3. success

После этого проверяется:

- что success действительно достигается;
- что было ровно нужное число попыток;
- что число попыток берется из конфига.

Также есть отдельная проверка stop-on-429: rate-limit не должен бессмысленно гоняться в retry-loop.

---

## 19. Как понять, что тест “хороший”

В этом проекте хороший тест обычно удовлетворяет четырем критериям:

### 19.1 Он проверяет бизнес-смысл, а не случайную реализацию

Например, лучше проверять:

- что valuation weight уменьшается при fallback baseline,

чем проверять:

- что внутри локальной переменной было конкретное промежуточное число на 17-й строке алгоритма.

### 19.2 Он воспроизводим

Тест не должен зависеть от:

- интернета;
- live Yahoo;
- live SEC;
- реальной БД;
- текущего дня;
- случайного порядка данных.

### 19.3 Он объясним

Если через полгода открыть тест, по его названию и данным должно быть понятно:

- что моделируется;
- зачем;
- почему ожидается именно такой результат.

### 19.4 Он проверяет деградацию, а не только идеальный happy path

В аналитических системах важны не только “хорошие” кейсы, но и:

- неполные данные;
- шумные peers;
- 429;
- timeout;
- weak baseline;
- fallback valuation.

---

## 20. Как добавить новый тест правильно

### 20.1 Если меняется формула или нормализация

Добавляй тест в:

- [unit/test_computation_core.py](/D:/Downloads/Dev/invest/backend/tests/unit/test_computation_core.py)

Лучший формат:

1. собрать минимальный вход;
2. вычислить expected value;
3. проверить точный или почти точный результат.

### 20.2 Если меняется провайдер или его fallback

Добавляй тест в:

- [integration/test_provider_integration.py](/D:/Downloads/Dev/invest/backend/tests/integration/test_provider_integration.py)

Лучший формат:

1. смоделировать ответ через `_JsonResponse` или `_SequenceClient`;
2. прогнать реальный provider method;
3. проверить payload, warnings, retry, fallback.

### 20.3 Если меняется бизнес-логика scoring или peer baseline

Обычно нужно:

- добавить regression case в [test_scoring_safety.py](/D:/Downloads/Dev/invest/backend/tests/test_scoring_safety.py)
- и/или добавить scenario test в [scenario/test_business_logic_scenarios.py](/D:/Downloads/Dev/invest/backend/tests/scenario/test_business_logic_scenarios.py)

### 20.4 Если нужен новый synthetic world

Добавляй dataset в:

- [support/factories.py](/D:/Downloads/Dev/invest/backend/tests/support/factories.py)

Лучше не конструировать большой словарь прямо в тесте. Намного удобнее:

1. создать именованный dataset builder;
2. дать ему осмысленное имя;
3. переиспользовать его в нескольких тестах.

### 20.5 Если нужно проверить endpoint

Используй:

- `api_client_factory` из [conftest.py](/D:/Downloads/Dev/invest/backend/tests/conftest.py)

Тогда endpoint тестируется через тот же dependency injection path, что и остальная система.

---

## 21. Как читать результат полного прогона

Когда ты запускаешь:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests -q
```

успешный результат означает, что одновременно прошли:

- формулы и edge cases;
- поведение провайдеров;
- cache/retry/fallback;
- сценарии peer-group;
- API wiring;
- bank/non-bank ветки;
- предупреждения и UI payload;
- regression safety pack.

Если падает unit-тест, это обычно означает локальную математическую или нормализационную ошибку.  
Если падает integration-тест, это обычно означает поломку взаимодействия между слоями.  
Если падает scenario-тест, это обычно означает бизнес-регрессию модели.  
Если падает `test_scoring_safety.py`, это часто сигнал, что где-то сдвинулись guardrails, threshold или fallback logic.

---

## 22. Как читать validation outputs

### 22.1 `score_validation`

Используй эту папку, когда нужно ответить на вопросы:

- почему score не выглядит произвольным;
- как он реагирует на небольшие изменения весов;
- как он реагирует на неполноту данных;
- разваливается ли ranking от небольших perturbation;
- как меняется valuation weight при ослаблении peer baseline.

### 22.2 `benchmark_diagnostics`

Используй эту папку, когда нужно ответить на вопросы:

- насколько быстро работает synthetic analysis path;
- насколько помогает cache;
- как система ведет себя в degraded mode;
- какие timeout/retry/TTL реально зафиксированы в системе.

---

## 23. Ограничения тестовой системы

Важно честно понимать, что тесты доказывают, а что нет.

### 23.1 Что тесты доказывают хорошо

- корректность логики на контролируемых входах;
- устойчивость к частичной недоступности источников;
- корректность fallback-веток;
- интерпретируемость peer baseline modes;
- отсутствие жесткой зависимости от live API;
- методическую стабильность score на synthetic scenarios.

### 23.2 Что тесты не доказывают автоматически

- инвестиционную истинность модели;
- абсолютную адекватность live market data;
- отсутствие любых проблем на всех реальных тикерах мира;
- production latency под реальной нагрузкой;
- корректность всех внешних API, если они изменят контракт.

Именно поэтому validation-слой формулируется как проверка устойчивости и внутренней непротиворечивости, а не как доказательство “истинности” инвестиционного вывода.

---

## 24. Практический маршрут для нового разработчика

Если нужно быстро понять, как устроены тесты, рекомендую такой порядок чтения:

1. [README.md](/D:/Downloads/Dev/invest/backend/tests/README.md)
2. [conftest.py](/D:/Downloads/Dev/invest/backend/tests/conftest.py)
3. [support/fakes.py](/D:/Downloads/Dev/invest/backend/tests/support/fakes.py)
4. [support/factories.py](/D:/Downloads/Dev/invest/backend/tests/support/factories.py)
5. [unit/test_computation_core.py](/D:/Downloads/Dev/invest/backend/tests/unit/test_computation_core.py)
6. [integration/test_provider_integration.py](/D:/Downloads/Dev/invest/backend/tests/integration/test_provider_integration.py)
7. [scenario/test_business_logic_scenarios.py](/D:/Downloads/Dev/invest/backend/tests/scenario/test_business_logic_scenarios.py)
8. [test_scoring_safety.py](/D:/Downloads/Dev/invest/backend/tests/test_scoring_safety.py)
9. [app/validation/common.py](/D:/Downloads/Dev/invest/backend/app/validation/common.py)
10. [app/validation/score_validation.py](/D:/Downloads/Dev/invest/backend/app/validation/score_validation.py)
11. [app/validation/benchmark_diagnostics.py](/D:/Downloads/Dev/invest/backend/app/validation/benchmark_diagnostics.py)

Если нужен самый быстрый старт:

1. запусти полный suite;
2. прочитай `conftest.py`;
3. прочитай один unit, один integration и один scenario test;
4. посмотри generated validation outputs.

---

## 25. Короткое итоговое резюме

Вся тестовая система backend построена вокруг одной идеи: использовать реальную бизнес-логику анализа, но кормить ее контролируемыми данными и подмененными зависимостями.

Поэтому тесты:

- быстрые;
- воспроизводимые;
- не требуют реального интернета;
- покрывают формулы, providers, peer pipeline, fallback baseline, API wiring и regression safety;
- дополнены validation-слоем с таблицами, графиками и численными метриками.

Если говорить совсем коротко, то логика такая:

- `support/` создает тестовый мир;
- `conftest.py` собирает из него сервисы и фикстуры;
- `unit/` проверяет формулы;
- `integration/` проверяет провайдеры и wiring;
- `scenario/` проверяет бизнес-смысл;
- `test_scoring_safety.py` страхует от регрессий;
- `app/validation/` превращает ту же инфраструктуру в исследовательские отчеты.
