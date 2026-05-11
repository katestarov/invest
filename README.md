# Investment Attractiveness

Учебный full-stack сервис для оценки инвестиционной привлекательности публичной компании по тикеру. Backend собирает рыночные, фундаментальные и макроэкономические данные, сравнивает компанию с похожими компаниями и возвращает итоговую оценку от `0` до `100`. Frontend показывает результат в виде понятной аналитической панели.

Проект не является инвестиционной рекомендацией. Его цель — показать, как можно собрать данные из разных источников, нормализовать показатели и прозрачно объяснить итоговый score.

## Что реализовано

- FastAPI backend.
- React + Vite frontend с поиском по тикеру, графиками, peer-table и детализацией score.
- Получение данных из Yahoo Finance, SEC EDGAR, FRED и World Bank.
- Подбор peer-group через внешние провайдеры и локальный fallback-конфиг.
- Расчет показателей `P/E`, `P/B`, `ROE`, `ROIC`, `Debt/Equity`, `Current Ratio`, маржинальности, роста и рыночной динамики.
- Сохранение слоев `bronze`, `silver`, `gold` в PostgreSQL.
- TTL-кеш для внешних provider-запросов и готового анализа.
- Набор unit, integration и scenario tests для ключевой бизнес-логики.

## Структура проекта

```text
backend/
  app/
    api/                 # HTTP routes
    config/              # scoring и peer-group настройки
    core/                # settings, database, logging
    db/                  # SQLAlchemy models
    repositories/        # сохранение bronze/silver/gold
    schemas/             # Pydantic response models
    services/            # расчет анализа и провайдеры данных
    utils/               # TTL cache
  tests/                 # автотесты backend
frontend/
  src/
    components/          # компоненты аналитической панели
    styles/              # стили приложения
docker-compose.yml       # PostgreSQL + backend + frontend
```

## Как работает анализ

1. Пользователь вводит тикер на frontend.
2. Backend проверяет кеш готового анализа.
3. Если кеша нет, сервис запрашивает внешние источники.
4. Сырые ответы сохраняются в слой `bronze`.
5. Данные приводятся к единому виду и сохраняются как `silver`.
6. Сервис подбирает peer-group и считает сравнительные показатели.
7. Формула из `backend/app/config/scoring.json` собирает итоговый score.
8. Готовый payload сохраняется в слой `gold` и возвращается frontend.

## Быстрый запуск

### Через Docker

```powershell
docker compose up --build
```

После запуска:

- frontend: http://localhost:5173
- backend healthcheck: http://localhost:8000/api/v1/health
- пример API: http://localhost:8000/api/v1/analyze/AAPL

### Backend локально

```powershell
cd backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

### Frontend локально

```powershell
cd frontend
npm install
npm run dev
```

## Настройка окружения

Шаблон переменных лежит в `backend/.env.example`. Основные параметры:

- `DATABASE_URL` — подключение к PostgreSQL.
- `SEC_USER_AGENT` — User-Agent для SEC EDGAR.
- `FRED_API_KEY` — ключ FRED, без него макроблок работает частично.
- `FMP_API_KEY` и `FINNHUB_API_KEY` — дополнительные источники для peer discovery.
- `ANALYSIS_CACHE_TTL_SECONDS` и `PROVIDER_CACHE_TTL_SECONDS` — время жизни кеша.

## Проверки

Backend:

```powershell
cd backend
pip install -r requirements-dev.txt
pytest
```

Frontend:

```powershell
cd frontend
npm run build
```

## Основные файлы

- `backend/app/services/analysis_runtime_service.py` — главный расчет анализа.
- `backend/app/services/providers/live_clients.py` — клиенты внешних источников.
- `backend/app/services/providers/peer_providers.py` — подбор peer-group.
- `backend/app/services/analysis_safety.py` — защитная логика для неполных и шумных данных.
- `backend/app/config/scoring.json` — веса и параметры скоринга.
- `backend/app/config/peer_groups.json` — локальные правила подбора похожих компаний.
- `frontend/src/App.tsx` — основной экран приложения.
