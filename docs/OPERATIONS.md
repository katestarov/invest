# Эксплуатация и запуск

## Backend

```powershell
cd D:\Downloads\Dev\invest\backend
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Frontend

```powershell
cd D:\Downloads\Dev\invest\frontend
npm install
npm run dev
```

## Production build frontend

Проверено в этой среде:

```powershell
& 'C:\Program Files\nodejs\node.exe' .\node_modules\typescript\bin\tsc -b
& 'C:\Program Files\nodejs\node.exe' .\node_modules\vite\bin\vite.js build
```

## Docker

```powershell
cd D:\Downloads\Dev\invest
docker compose up --build
```

## Переменные окружения

Используются:

- `DATABASE_URL`
- `FRED_API_KEY`
- `SEC_USER_AGENT`
- `FRONTEND_ORIGIN`
- `ANALYSIS_CACHE_TTL_SECONDS`
- `PROVIDER_CACHE_TTL_SECONDS`

## Проверки, которые уже прошли

- компиляция Python-кода backend
- импорт FastAPI-приложения
- live backend-анализ по реальному тикеру
- `tsc -b`
- `vite build`

