from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.core.database import Base, engine
from app.core.settings import get_settings

settings = get_settings()

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

app.include_router(router, prefix="/api/v1")


@app.on_event("startup")
def on_startup() -> None:
    try:
        Base.metadata.create_all(bind=engine)
    except Exception:
        # Allow the API to start even if PostgreSQL is temporarily unavailable.
        pass


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "Investment Attractiveness API is running"}
