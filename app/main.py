import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import get_settings
from app.database import async_session, engine
from app.redis import close_redis, get_redis
from app.routers import auth, users
from app.ws.chat import router as ws_router, start_subscriber, stop_subscriber

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("whisper")

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: launch Redis subscriber
    await start_subscriber()
    logger.info("Redis pub/sub subscriber started")

    yield

    # Shutdown
    await stop_subscriber()
    await engine.dispose()
    await close_redis()
    logger.info("Shutdown complete")


_is_dev = settings.ENVIRONMENT == "development"

app = FastAPI(
    title="Whisper",
    description="E2EE secure chat backend",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if _is_dev else None,
    redoc_url=None,
)

_cors_origins: list[str] = (
    [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
    if settings.CORS_ORIGINS
    else []
)

if _is_dev and not _cors_origins:
    _cors_origins = ["*"]

if not _cors_origins:
    logger.warning("CORS_ORIGINS is empty — no cross-origin requests will be allowed")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(ws_router)


@app.get("/health")
async def health():
    """Liveness probe — confirms the process is running."""
    return {"status": "ok"}


@app.get("/ready")
async def readiness():
    """Readiness probe — confirms DB and Redis are reachable."""
    errors: list[str] = []

    try:
        async with async_session() as db:
            await db.execute(text("SELECT 1"))
    except Exception:
        errors.append("postgres")

    try:
        r = get_redis()
        try:
            await r.ping()
        finally:
            await r.aclose()
    except Exception:
        errors.append("redis")

    if errors:
        return JSONResponse(
            {"status": "unavailable", "failing": errors},
            status_code=503,
        )

    return {"status": "ready"}
