# Backend Test Infrastructure

Тесты backend теперь разделены по назначению:

- `tests/unit/` — точечные проверки вычислительного ядра, нормализации и provider-level расчетов
- `tests/integration/` — проверки FastAPI и wiring через dependency injection
- `tests/scenario/` — сквозные сценарии `AnalysisService` на mock-данных без реальных HTTP-вызовов
- `tests/support/` — общие fake providers, payload builders и dataset-фабрики

## Как запускать

Из корня репозитория:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests -q
```

Только unit:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests/unit -q
```

Только integration и scenario:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests/integration backend/tests/scenario -q
```

Если `pytest` ещё не установлен в виртуальном окружении, поставь dev-зависимости из `backend/requirements-dev.txt`.

## Принципы

- Активный runtime path через `analysis_runtime_service.py` не меняется по поведению и тестируется через injection.
- Реальные HTTP-запросы для unit/integration/scenario тестов не нужны.
- Persistence в тестах можно отключать или подменять через `RecordingPersistence`.
- FastAPI endpoint можно подменять через `app.dependency_overrides[routes.get_analysis_service]`.

## Навигация по отчетам

- подробное руководство по всей тестовой системе: [TESTING_DEEP_DIVE_RU.md](/D:/Downloads/Dev/invest/backend/tests/TESTING_DEEP_DIVE_RU.md)
- матрица интеграционных сценариев провайдеров: [integration/INTEGRATION_PROVIDER_MATRIX.md](/D:/Downloads/Dev/invest/backend/tests/integration/INTEGRATION_PROVIDER_MATRIX.md)
- отчет по вычислительному ядру: [unit/COMPUTATION_CORE_REPORT.md](/D:/Downloads/Dev/invest/backend/tests/unit/COMPUTATION_CORE_REPORT.md)
- краткое описание сквозных бизнес-сценариев: [scenario/SCENARIO_SUMMARY.md](/D:/Downloads/Dev/invest/backend/tests/scenario/SCENARIO_SUMMARY.md)
