import secrets
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

# Lua script for atomic rate limiting: INCR + conditional EXPIRE in one round-trip
_RATE_LIMIT_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
"""


async def check_rate_limit(key: str, limit: int, window: int) -> None:
    """Atomic sliding-window counter rate limiter."""
    r = get_redis()
    try:
        current = await r.eval(_RATE_LIMIT_SCRIPT, 1, key, window)
        if int(current) > limit:
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
