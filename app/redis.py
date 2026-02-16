import redis.asyncio as redis

from app.config import get_settings

settings = get_settings()

redis_pool = redis.ConnectionPool.from_url(
    settings.REDIS_URL,
    decode_responses=True,
    max_connections=50,
)


def get_redis() -> redis.Redis:
    return redis.Redis(connection_pool=redis_pool)


async def close_redis():
    await redis_pool.aclose()
