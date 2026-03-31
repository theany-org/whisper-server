import base64
import hashlib
import hmac
import ipaddress
import logging
import math
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.middleware.auth import (
    check_rate_limit,
    create_ws_ticket,
    delete_session,
    get_current_user_id,
    store_session,
)
from app.models.user import User
from app.schemas.auth import (
    LoginRequest,
    MessageResponse,
    RegisterRequest,
    TokenResponse,
)
from app.security.jwt import create_access_token
from app.security.password import DUMMY_HASH, hash_password, verify_password

logger = logging.getLogger("whisper.auth")
settings = get_settings()

router = APIRouter(prefix="/auth", tags=["auth"])

# Parse trusted proxy networks once at import time
_trusted_proxies: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
for cidr in settings.TRUSTED_PROXIES.split(","):
    cidr = cidr.strip()
    if cidr:
        _trusted_proxies.append(ipaddress.ip_network(cidr, strict=False))


def _get_client_ip(request: Request) -> str:
    """Extract client IP, only trusting X-Forwarded-For from known proxies."""
    direct_ip = request.client.host if request.client else "unknown"

    if not _trusted_proxies:
        return direct_ip

    try:
        addr = ipaddress.ip_address(direct_ip)
    except ValueError:
        return direct_ip

    if not any(addr in net for net in _trusted_proxies):
        return direct_ip

    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        candidate = forwarded.split(",")[0].strip()
        try:
            ipaddress.ip_address(candidate)
            return candidate
        except ValueError:
            return direct_ip

    return direct_ip


@router.post(
    "/register",
    response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    body: RegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    client_ip = _get_client_ip(request)
    await check_rate_limit(
        f"register_rl:{client_ip}",
        settings.LOGIN_RATE_LIMIT,
        settings.LOGIN_RATE_WINDOW,
    )

    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        public_key=body.public_key,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already taken",
        )

    logger.info("User registered: %s", body.username)
    return MessageResponse(message="Registration successful")


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    # Rate limit by IP to slow brute-force attacks
    client_ip = _get_client_ip(request)
    await check_rate_limit(
        f"login_rl:{client_ip}",
        settings.LOGIN_RATE_LIMIT,
        settings.LOGIN_RATE_WINDOW,
    )

    # Constant-time-ish lookup: always hash-check even if user not found
    # to prevent username enumeration via timing side-channel.
    result = await db.execute(select(User).where(User.username == body.username))
    user = result.scalar_one_or_none()

    if user is None:
        # Single bcrypt verify against precomputed hash for constant-time response
        verify_password("dummy", DUMMY_HASH)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    if not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    token = create_access_token(user.id)
    await store_session(user.id, token)

    logger.info("User logged in: %s", user.username)
    return TokenResponse(access_token=token)


@router.post("/logout", response_model=MessageResponse)
async def logout(
    current_user_id: uuid.UUID = Depends(get_current_user_id),
):
    await delete_session(current_user_id)
    logger.info("User logged out: %s", current_user_id)
    return MessageResponse(message="Logged out")


@router.post("/ws-ticket")
async def issue_ws_ticket(
    current_user_id: uuid.UUID = Depends(get_current_user_id),
):
    """Issue a short-lived, single-use ticket for WebSocket authentication."""
    ticket = await create_ws_ticket(current_user_id)
    return {"ticket": ticket}


@router.post("/turn-credentials")
async def get_turn_credentials(
    current_user_id: uuid.UUID = Depends(get_current_user_id),
):
    """Return time-limited TURN credentials signed with the coturn static-auth-secret.

    Uses the TURN REST API credential format:
    username = "{expiry_unix}:{user_id}"
    credential = base64(HMAC-SHA1(secret, username))
    """
    if not settings.COTURN_SECRET:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="TURN server not configured",
        )

    expiry = math.floor(time.time()) + 3600  # valid for 1 hour
    username = f"{expiry}:{current_user_id}"
    credential = base64.b64encode(
        hmac.new(
            settings.COTURN_SECRET.encode(),
            username.encode(),
            hashlib.sha1,
        ).digest()
    ).decode()

    return {
        "urls": [
            f"stun:{settings.COTURN_REALM}:3478",
            f"turn:{settings.COTURN_REALM}:3478",
        ],
        "username": username,
        "credential": credential,
    }
