import asyncio
import json
import logging
import time
import uuid

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.database import async_session
from app.middleware.auth import redeem_ws_ticket
from app.models.user import User
from app.redis import get_redis

logger = logging.getLogger("whisper.ws")

router = APIRouter()

# In-memory map: user_id (str) -> WebSocket
_connections: dict[str, WebSocket] = {}

# username -> user_id for quick presence lookups
_online_users: dict[str, str] = {}

REDIS_CHANNEL = "whisper:messages"

# Strong reference to prevent garbage collection (Python asyncio only keeps weak refs)
_subscriber_task: asyncio.Task | None = None

# Maximum allowed length for ciphertext and nonce (base64-encoded)
MAX_CIPHERTEXT_LEN = 65_536  # ~48 KB of raw ciphertext
MAX_NONCE_LEN = 64  # NaCl nonce is 24 bytes → 32 chars base64


async def _resolve_username_to_id(username: str) -> str | None:
    async with async_session() as db:
        result = await db.execute(
            select(User.id).where(User.username == username.lower())
        )
        row = result.scalar_one_or_none()
        return str(row) if row else None


async def _resolve_id_to_username(user_id: uuid.UUID) -> str | None:
    async with async_session() as db:
        result = await db.execute(select(User.username).where(User.id == user_id))
        return result.scalar_one_or_none()


def is_user_online(username: str) -> bool:
    return username in _online_users


async def _deliver_local(target_id: str, payload: dict) -> bool:
    """Deliver to a locally connected WebSocket. Returns True if delivered."""
    ws = _connections.get(target_id)
    if ws is None:
        return False
    try:
        await ws.send_text(json.dumps(payload))
        return True
    except Exception:
        _connections.pop(target_id, None)
        return False


async def _redis_subscriber():
    """Listen on Redis pub/sub and deliver messages to local connections."""
    while True:
        r = get_redis()
        pubsub = r.pubsub()
        try:
            await pubsub.subscribe(REDIS_CHANNEL)
            logger.info("Redis subscriber listening on %s", REDIS_CHANNEL)
            async for raw in pubsub.listen():
                if raw["type"] != "message":
                    continue
                try:
                    envelope = json.loads(raw["data"])
                    target_id = envelope.get("target_id")
                    await _deliver_local(target_id, envelope["payload"])
                except Exception:
                    logger.exception("Error processing pub/sub message")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Redis subscriber crashed, restarting in 2s")
            await asyncio.sleep(2)
        finally:
            try:
                await pubsub.unsubscribe(REDIS_CHANNEL)
                await pubsub.aclose()
            except Exception:
                pass
            await r.aclose()


@router.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket, ticket: str = Query(...)):
    # Redeem the single-use ticket (replaces raw JWT in query string)
    user_id = await redeem_ws_ticket(ticket)
    if user_id is None:
        await websocket.accept()
        await websocket.close(code=4001, reason="Authentication failed")
        return

    await websocket.accept()

    uid_str = str(user_id)
    sender_name = await _resolve_id_to_username(user_id)

    # Close any existing connection for this user before overwriting
    old_ws = _connections.get(uid_str)
    if old_ws is not None:
        try:
            await old_ws.close(code=4002, reason="Superseded by new connection")
        except Exception:
            pass

    _connections[uid_str] = websocket
    if sender_name:
        _online_users[sender_name] = uid_str
    logger.info("WS connected: %s", sender_name)

    try:
        while True:
            raw = await websocket.receive_text()

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({"error": "Invalid JSON"}))
                continue

            # Validate required fields
            recipient = msg.get("to")
            ciphertext = msg.get("ciphertext")
            nonce = msg.get("nonce")

            if not all([recipient, ciphertext, nonce]):
                await websocket.send_text(
                    json.dumps(
                        {"error": "Missing required fields: to, ciphertext, nonce"}
                    )
                )
                continue

            if not isinstance(ciphertext, str) or not isinstance(nonce, str):
                await websocket.send_text(
                    json.dumps({"error": "ciphertext and nonce must be strings"})
                )
                continue

            # Enforce payload size limits
            if len(ciphertext) > MAX_CIPHERTEXT_LEN:
                await websocket.send_text(
                    json.dumps({"error": "ciphertext exceeds maximum allowed size"})
                )
                continue

            if len(nonce) > MAX_NONCE_LEN:
                await websocket.send_text(
                    json.dumps({"error": "nonce exceeds maximum allowed size"})
                )
                continue

            target_id = await _resolve_username_to_id(recipient)
            if target_id is None:
                await websocket.send_text(json.dumps({"error": "Recipient not found"}))
                continue

            payload = {
                "from": sender_name,
                "ciphertext": ciphertext,
                "nonce": nonce,
                "timestamp": int(time.time()),
            }

            # Try direct local delivery first
            delivered = await _deliver_local(target_id, payload)
            if not delivered:
                envelope = json.dumps({"target_id": target_id, "payload": payload})
                r = get_redis()
                try:
                    await r.publish(REDIS_CHANNEL, envelope)
                finally:
                    await r.aclose()

            # Notify sender of delivery status
            await websocket.send_text(
                json.dumps({
                    "type": "status",
                    "to": recipient,
                    "delivered": delivered,
                    "timestamp": payload["timestamp"],
                })
            )

            if delivered:
                logger.info("Message relayed: %s -> %s", sender_name, recipient)

    except WebSocketDisconnect:
        logger.info("WS disconnected: %s", sender_name)
    finally:
        # Only remove if this socket is still the current one (not already superseded)
        if _connections.get(uid_str) is websocket:
            _connections.pop(uid_str, None)
        if sender_name:
            _online_users.pop(sender_name, None)


async def start_subscriber():
    """Spawn the Redis subscriber as a background task with a strong reference."""
    global _subscriber_task
    _subscriber_task = asyncio.create_task(_redis_subscriber())


async def stop_subscriber():
    """Cancel and await the Redis subscriber task for clean shutdown."""
    global _subscriber_task
    if _subscriber_task is not None:
        _subscriber_task.cancel()
        try:
            await _subscriber_task
        except asyncio.CancelledError:
            pass
        _subscriber_task = None
        logger.info("Redis subscriber stopped")
