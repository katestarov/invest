# Investment Attractiveness MVP

Сервис оценки инвестиционной привлекательности компаний по тикеру. Приложение собирает рыночные, фундаментальные и макроэкономические данные, сравнивает компанию с peer-group внутри ее сектора и формирует итоговую оценку в шкале от `0` до `100`.

## Что внутри

- `backend/` — FastAPI API, провайдеры данных, ETL-логика, PostgreSQL-слои и кеширование
- `frontend/` — React + Vite интерфейс с графиками, таблицами и поиском по тикеру
- `docs/` — подробная документация по архитектуре, источникам и эксплуатации
- `docker-compose.yml` — запуск `postgres + backend + frontend`

## Основные возможности

- живые HTTP-источники вместо демо-адаптеров
- хранение слоев `bronze / silver / gold` в PostgreSQL
- конфигурируемая формула скоринга
- rules-based подбор `peer-group`
- кеширование провайдеров и готового анализа
- история фундаментальных показателей
- история цены акции
- русскоязычный интерфейс с английскими терминами только в скобках

## Источники данных

### Yahoo Finance

Используется для:

- текущей цены акции
- истории месячных цен
- доходности за 1 год
- доходности за 5 лет

### SEC EDGAR

Используется для:

- официальных фундаментальных показателей
- выручки
- чистой и операционной прибыли
- активов, обязательств и капитала
- операционного денежного потока и capex
- числа акций в обращении

На базе SEC рассчитываются:

- `P/E (Price/Earnings)`
- `P/B (Price/Book)`
- `ROE (Return on Equity)`
- `ROIC (Return on Invested Capital)`
- `Debt/Equity`
- `Current Ratio`
- `EBIT Margin`
- `FCF Margin`

### FRED

Используется для:

- ставки ФРС
- инфляции
- безработицы

### World Bank

Используется для:

- роста ВВП США

## Как работает сервис

1. Пользователь вводит тикер.
2. API проверяет кеш готового анализа.
3. Если кеша нет, сервис идет во внешние источники.
4. Сырые ответы сохраняются в `bronze`.
5. Подбирается `peer-group` по сектору и индустрии.
6. Считаются нормализованные `silver`-метрики.
7. Применяется формула скоринга из конфига.
8. Готовый результат сохраняется в `gold`.
9. Frontend получает уже готовую витрину и отображает ее.

## Архитектура слоев данных

### Bronze

Сырые ответы внешних API:

- таблица `bronze_snapshots`

### Silver

Нормализованные показатели и peer snapshot:

- таблица `silver_analyses`

### Gold

Итоговая оценка и финальный payload для UI:

- таблица `gold_scores`

## Кеширование

### Кеш провайдеров

TTL-кеширует внешние HTTP-ответы по `url + params`.

Настройка:

- `PROVIDER_CACHE_TTL_SECONDS`

### Кеш анализа

TTL-кеширует готовый `AnalysisResponse` по тикеру.

Настройка:

- `ANALYSIS_CACHE_TTL_SECONDS`

### Сброс кеша

Endpoint:

- `POST /api/v1/cache/clear`

## Конфиги

- формула скоринга: [backend/app/config/scoring.json](/D:/Downloads/Dev/invest/backend/app/config/scoring.json)
- правила `peer-group`: [backend/app/config/peer_groups.json](/D:/Downloads/Dev/invest/backend/app/config/peer_groups.json)

## Переменные окружения

Файл-шаблон:

- [backend/.env.example](/D:/Downloads/Dev/invest/backend/.env.example)

Основные переменные:

- `DATABASE_URL`
- `FRED_API_KEY`
- `SEC_USER_AGENT`
- `FRONTEND_ORIGIN`
- `ANALYSIS_CACHE_TTL_SECONDS`
- `PROVIDER_CACHE_TTL_SECONDS`

## Быстрый запуск

### Backend

```powershell
cd D:\Downloads\Dev\invest\backend
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

### Frontend

```powershell
cd D:\Downloads\Dev\invest\frontend
npm install
npm run dev
```

### Docker

```powershell
cd D:\Downloads\Dev\invest
docker compose up --build
```

## Что уже проверено

- backend Python compile — успешно
- live backend-анализ по реальному тикеру — успешно
- frontend `tsc -b` — успешно
- frontend `vite build` — успешно

## Подробная документация

- архитектура и алгоритм: [docs/SYSTEM.md](/D:/Downloads/Dev/invest/docs/SYSTEM.md)
- запуск и эксплуатация: [docs/OPERATIONS.md](/D:/Downloads/Dev/invest/docs/OPERATIONS.md)
