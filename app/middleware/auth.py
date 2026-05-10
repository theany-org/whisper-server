import secrets
import time
import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings
from app.redis import get_redis
from app.security.jwt import decode_access_token

_bearer = HTTPBearer(auto_error=False)

SESSION_TTL = 7 * 24 * 3600  # 7 days in seconds

settings = get_settings()


# ── Redis session helpers ──────────────────────────────────────────────

def _session_key(user_id: str) -> str:
    return f"session:{user_id}"


async def store_session(user_id: uuid.UUID, token: str) -> None:
    # Single-session by design: each new login overwrites the previous token,
    # immediately invalidating any existing session on another device.
    # This mirrors Signal's desktop model — one active session per user.
    # To support multi-device, replace SET with SADD into a per-user token set
    # and update session_exists / delete_session accordingly.
    r = get_redis()
    try:
        await r.set(_session_key(str(user_id)), token, ex=SESSION_TTL)
    finally:
        await r.aclose()


async def session_exists(user_id: str, token: str) -> bool:
    r = get_redis()
    try:
        stored = await r.get(_session_key(user_id))
        return stored == token
    finally:
        await r.aclose()


async def delete_session(user_id: uuid.UUID) -> None:
    r = get_redis()
    try:
        await r.delete(_session_key(str(user_id)))
    finally:
        await r.aclose()


# ── WebSocket ticket helpers ──────────────────────────────────────────

def _ticket_key(ticket: str) -> str:
    return f"ws_ticket:{ticket}"


async def create_ws_ticket(user_id: uuid.UUID) -> str:
    """Create a short-lived, single-use ticket that maps to a user_id."""
    ticket = secrets.token_urlsafe(32)
    r = get_redis()
    try:
        await r.set(
            _ticket_key(ticket),
            str(user_id),
            ex=settings.WS_TICKET_TTL,
        )
    finally:
        await r.aclose()
    return ticket


async def redeem_ws_ticket(ticket: str) -> uuid.UUID | None:
    """Consume a ticket atomically. Returns user_id or None if invalid/expired."""
    r = get_redis()
    try:
        # GETDEL is atomic — the ticket can only be used once
        user_id_str = await r.getdel(_ticket_key(ticket))
        if user_id_str is None:
            return None
        return uuid.UUID(user_id_str)
    finally:
        await r.aclose()


# ── Rate limiter helpers ───────────────────────────────────────────────

# True sliding-window rate limiter using a Redis sorted set.
# Each request is stored as a member scored by its millisecond timestamp.
# On every call: expired members (older than the window) are pruned first,
# then the remaining count is checked against the limit.
# This prevents the fixed-window boundary burst (2× limit in 2 requests) that
# the previous INCR+EXPIRE approach allowed.
#
# ARGV[1] = now_ms   — current time in milliseconds
# ARGV[2] = win_ms   — window size in milliseconds
# ARGV[3] = limit    — max requests allowed in the window
# Returns 1 if the request is allowed, 0 if rate-limited.
_RATE_LIMIT_SCRIPT = """
local now_ms = tonumber(ARGV[1])
local win_ms = tonumber(ARGV[2])
local limit  = tonumber(ARGV[3])

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now_ms - win_ms)
local count = redis.call('ZCARD', KEYS[1])
if count >= limit then
    return 0
end
redis.call('ZADD', KEYS[1], now_ms, now_ms .. math.random(1000000000))
redis.call('PEXPIRE', KEYS[1], win_ms)
return 1
"""


async def check_rate_limit(key: str, limit: int, window: int) -> None:
    """Sliding-window rate limiter. Raises 429 if the limit is exceeded."""
    now_ms = int(time.time() * 1000)
    r = get_redis()
    try:
        allowed = await r.eval(_RATE_LIMIT_SCRIPT, 1, key, now_ms, window * 1000, limit)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Try again later.",
            )
    finally:
        await r.aclose()


# ── FastAPI dependency ─────────────────────────────────────────────────

async def get_current_user_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> uuid.UUID:
    """Validate JWT + verify active Redis session. Returns user UUID."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    token = credentials.credentials
    payload = decode_access_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )

    if not await session_exists(user_id, token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or invalidated",
        )

    return uuid.UUID(user_id)
