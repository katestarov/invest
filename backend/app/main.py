from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import routes
from app.api.routes import router
from app.core.database import Base, engine
from app.core.logging_config import configure_logging
from app.core.settings import get_settings
from app.middleware.request_context import correlation_id_middleware

settings = get_settings()
configure_logging(settings.log_level)

app = FastAPI(
    title="Investment Attractiveness API",
    version="0.1.0",
    description="MVP API for evaluating a company's investment attractiveness against sector peers.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin, "http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.middleware("http")(correlation_id_middleware)

app.include_router(router, prefix="/api/v1")


@app.on_event("startup")
def on_startup() -> None:
    try:
        Base.metadata.create_all(bind=engine)
    except Exception:
        # Allow the API to start even if PostgreSQL is temporarily unavailable.
        pass


@app.on_event("shutdown")
def on_shutdown() -> None:
    routes.service.close()


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "Investment Attractiveness API is running"}
