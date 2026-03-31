import asyncio
import json
import logging
import secrets
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

# In-memory map: user_id (str) -> WebSocket (local to this worker)
_connections: dict[str, WebSocket] = {}

# username -> user_id for quick presence lookups (local to this worker)
_online_users: dict[str, str] = {}

REDIS_CHANNEL = "whisper:messages"
PRESENCE_CHANNEL = "whisper:presence"

OFFLINE_QUEUE_PREFIX = "offline:"
OFFLINE_QUEUE_TTL = 7 * 24 * 3600  # 7 days

# Redis presence keys — cross-worker online tracking
# ws:online:{user_id} = "1"  (TTL = PRESENCE_TTL, refreshed on connect)
PRESENCE_PREFIX = "ws:online:"
PRESENCE_TTL = 24 * 3600  # 24h — deleted on clean disconnect, expires as crash fallback

# Redis keys for call state
CALL_USER_PREFIX = "call:user:"
CALL_PARTS_PREFIX = "call:parts:"
CALL_STATE_TTL = 3600  # 1 hour safety expiry

# Strong reference to prevent garbage collection
_subscriber_task: asyncio.Task | None = None

# Maximum allowed length for ciphertext and nonce (base64-encoded)
MAX_CIPHERTEXT_LEN = 716_800
MAX_NONCE_LEN = 64


# ─── DB helpers ──────────────────────────────────────────────────────────────


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


# ─── Redis presence helpers (cross-worker) ────────────────────────────────────


async def _set_presence(user_id: str) -> None:
    """Mark user as online in Redis so all workers can see it."""
    r = get_redis()
    try:
        await r.set(f"{PRESENCE_PREFIX}{user_id}", "1", ex=PRESENCE_TTL)
    finally:
        await r.aclose()


async def _clear_presence(user_id: str) -> None:
    """Remove online marker when user disconnects."""
    r = get_redis()
    try:
        await r.delete(f"{PRESENCE_PREFIX}{user_id}")
    finally:
        await r.aclose()


async def _is_online(user_id: str) -> bool:
    """Check if a user is online on ANY worker via Redis."""
    r = get_redis()
    try:
        return bool(await r.exists(f"{PRESENCE_PREFIX}{user_id}"))
    finally:
        await r.aclose()


async def _get_online_usernames() -> list[str]:
    """Return usernames of all currently online users (cross-worker, via Redis)."""
    r = get_redis()
    try:
        user_ids: list[uuid.UUID] = []
        async for key in r.scan_iter(f"{PRESENCE_PREFIX}*"):
            try:
                user_ids.append(uuid.UUID(key[len(PRESENCE_PREFIX) :]))
            except ValueError:
                pass
        if not user_ids:
            return []
        async with async_session() as db:
            result = await db.execute(
                select(User.username).where(User.id.in_(user_ids))
            )
            return [row[0] for row in result.fetchall()]
    finally:
        await r.aclose()


async def _broadcast_presence(username: str, *, online: bool) -> None:
    """Publish a presence event so all workers push it to their local clients."""
    r = get_redis()
    try:
        event = json.dumps(
            {
                "type": "user_online" if online else "user_offline",
                "username": username,
            }
        )
        await r.publish(PRESENCE_CHANNEL, event)
    finally:
        await r.aclose()


# ─── Redis call-state helpers ─────────────────────────────────────────────────


async def _set_user_in_call(
    user_id: str, call_id: str, caller_id: str, callee_id: str
) -> None:
    r = get_redis()
    try:
        await r.set(f"{CALL_USER_PREFIX}{user_id}", call_id, ex=CALL_STATE_TTL)
        await r.set(
            f"{CALL_PARTS_PREFIX}{call_id}",
            f"{caller_id}:{callee_id}",
            ex=CALL_STATE_TTL,
        )
    finally:
        await r.aclose()


async def _get_user_call(user_id: str) -> str | None:
    r = get_redis()
    try:
        return await r.get(
            f"{CALL_USER_PREFIX}{user_id}"
        )  # already str (decode_responses=True)
    finally:
        await r.aclose()


async def _get_call_participants(call_id: str) -> tuple[str, str] | None:
    r = get_redis()
    try:
        val = await r.get(f"{CALL_PARTS_PREFIX}{call_id}")
        if not val:
            return None
        parts = val.split(":", 1)
        return (parts[0], parts[1]) if len(parts) == 2 else None
    finally:
        await r.aclose()


async def _end_call_state(call_id: str) -> None:
    r = get_redis()
    try:
        val = await r.get(f"{CALL_PARTS_PREFIX}{call_id}")
        await r.delete(f"{CALL_PARTS_PREFIX}{call_id}")
        if val:
            caller_id, callee_id = val.split(":", 1)
            await r.delete(f"{CALL_USER_PREFIX}{caller_id}")
            await r.delete(f"{CALL_USER_PREFIX}{callee_id}")
    finally:
        await r.aclose()


# ─── Offline queue ────────────────────────────────────────────────────────────


async def _queue_offline(target_id: str, payload: dict) -> None:
    r = get_redis()
    try:
        key = f"{OFFLINE_QUEUE_PREFIX}{target_id}"
        await r.rpush(key, json.dumps(payload))
        await r.expire(key, OFFLINE_QUEUE_TTL)
    finally:
        await r.aclose()
    logger.info("Queued offline message for user %s", target_id)


async def _flush_offline_queue(user_id: str, websocket: WebSocket) -> None:
    r = get_redis()
    try:
        key = f"{OFFLINE_QUEUE_PREFIX}{user_id}"
        count = 0
        while True:
            item = await r.lpop(key)
            if item is None:
                break
            try:
                payload = json.loads(item)
                await websocket.send_text(json.dumps(payload))
                count += 1
            except Exception:
                await r.lpush(key, item)
                logger.exception(
                    "Failed to deliver offline message to %s; re-queued", user_id
                )
                break
        if count:
            logger.info("Flushed %d offline message(s) to %s", count, user_id)
    finally:
        await r.aclose()


# ─── Delivery helpers ─────────────────────────────────────────────────────────


async def _deliver_local(target_id: str, payload: dict) -> bool:
    """Deliver directly to a WebSocket on this worker. Returns True if delivered."""
    ws = _connections.get(target_id)
    if ws is None:
        return False
    try:
        await ws.send_text(json.dumps(payload))
        return True
    except Exception:
        _connections.pop(target_id, None)
        return False


async def _relay(target_id: str, payload: dict, queue_if_offline: bool = True) -> bool:
    """Try local delivery, then Redis pub/sub. Optionally queue offline for chat messages."""
    delivered = await _deliver_local(target_id, payload)
    if delivered:
        return True

    r = get_redis()
    try:
        envelope = json.dumps(
            {
                "target_id": target_id,
                "payload": payload,
                "queue_if_offline": queue_if_offline,
            }
        )
        receivers = await r.publish(REDIS_CHANNEL, envelope)
        if receivers == 0 and queue_if_offline:
            await _queue_offline(target_id, payload)
        return receivers > 0 or not queue_if_offline
    finally:
        await r.aclose()


async def _send_error(websocket: WebSocket, message: str) -> None:
    await websocket.send_text(json.dumps({"error": message}))


# ─── Redis pub/sub subscriber ─────────────────────────────────────────────────


async def _redis_subscriber():
    """Deliver messages, call signals, and presence events published by other workers."""
    while True:
        r = get_redis()
        pubsub = r.pubsub()
        try:
            await pubsub.subscribe(REDIS_CHANNEL, PRESENCE_CHANNEL)
            logger.info(
                "Redis subscriber listening on %s + %s", REDIS_CHANNEL, PRESENCE_CHANNEL
            )
            async for raw in pubsub.listen():
                if raw["type"] != "message":
                    continue
                try:
                    channel = raw["channel"]

                    if channel == PRESENCE_CHANNEL:
                        # Broadcast presence event to every local connection
                        payload = json.loads(raw["data"])
                        for ws in list(_connections.values()):
                            try:
                                await ws.send_text(json.dumps(payload))
                            except Exception:
                                pass
                        continue

                    # Targeted delivery (chat messages + call signals)
                    envelope = json.loads(raw["data"])
                    target_id = envelope.get("target_id")
                    payload = envelope["payload"]
                    queue_if_offline = envelope.get("queue_if_offline", True)

                    delivered = await _deliver_local(target_id, payload)
                    if not delivered and queue_if_offline:
                        msg_id = payload.get("msg_id")
                        should_queue = True
                        if msg_id:
                            r_dedup = get_redis()
                            try:
                                acquired = await r_dedup.set(
                                    f"offline_dedup:{msg_id}",
                                    "1",
                                    nx=True,
                                    ex=60,
                                )
                                should_queue = bool(acquired)
                            finally:
                                await r_dedup.aclose()
                        if should_queue:
                            await _queue_offline(target_id, payload)
                    # Call signals (queue_if_offline=False) are silently dropped
                    # if the target is not on this worker — another worker will deliver.
                except Exception:
                    logger.exception("Error processing pub/sub message")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Redis subscriber crashed, restarting in 2s")
            await asyncio.sleep(2)
        finally:
            try:
                await pubsub.unsubscribe(REDIS_CHANNEL, PRESENCE_CHANNEL)
                await pubsub.aclose()
            except Exception:
                pass
            await r.aclose()


# ─── Chat message handler ─────────────────────────────────────────────────────


async def _handle_chat(
    websocket: WebSocket,
    msg: dict,
    uid_str: str,
    sender_name: str,
) -> None:
    recipient = msg.get("to")
    ciphertext = msg.get("ciphertext")
    nonce = msg.get("nonce")

    if not all([recipient, ciphertext, nonce]):
        await _send_error(websocket, "Missing required fields: to, ciphertext, nonce")
        return

    if not isinstance(ciphertext, str) or not isinstance(nonce, str):
        await _send_error(websocket, "ciphertext and nonce must be strings")
        return

    if len(ciphertext) > MAX_CIPHERTEXT_LEN:
        await _send_error(websocket, "ciphertext exceeds maximum allowed size")
        return

    if len(nonce) > MAX_NONCE_LEN:
        await _send_error(websocket, "nonce exceeds maximum allowed size")
        return

    target_id = await _resolve_username_to_id(recipient)
    if target_id is None:
        await _send_error(websocket, "Recipient not found")
        return

    msg_type = msg.get("type", "chat_message")
    duration = msg.get("duration")

    payload = {
        "msg_id": secrets.token_hex(16),
        "from": sender_name,
        "type": msg_type,
        "ciphertext": ciphertext,
        "nonce": nonce,
        "timestamp": int(time.time()),
    }
    if duration is not None:
        payload["duration"] = duration

    await _relay(target_id, payload, queue_if_offline=True)

    await websocket.send_text(
        json.dumps(
            {
                "type": "status",
                "to": recipient,
                "delivered": True,
                "timestamp": payload["timestamp"],
                "msg_id": payload["msg_id"],
            }
        )
    )

    logger.info("Chat relayed: %s -> %s", sender_name, recipient)


# ─── Call signal handlers ─────────────────────────────────────────────────────


async def _handle_call_offer(
    websocket: WebSocket,
    msg: dict,
    uid_str: str,
    sender_name: str,
) -> None:
    call_id = msg.get("call_id")
    to = msg.get("to")
    sdp = msg.get("sdp")

    if not all([call_id, to, sdp]):
        await _send_error(websocket, "call_offer requires call_id, to, sdp")
        return

    target_id = await _resolve_username_to_id(to)
    if target_id is None:
        await _send_error(websocket, "Recipient not found")
        return

    # Use Redis presence — works across all Gunicorn workers
    if not await _is_online(target_id):
        await websocket.send_text(
            json.dumps(
                {
                    "type": "call_unavailable",
                    "call_id": call_id,
                }
            )
        )
        return

    callee_call = await _get_user_call(target_id)
    if callee_call:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "call_busy",
                    "call_id": call_id,
                    "from": to,
                }
            )
        )
        return

    caller_call = await _get_user_call(uid_str)
    if caller_call:
        await _send_error(websocket, "You are already in a call")
        return

    await _set_user_in_call(uid_str, call_id, uid_str, target_id)
    await _set_user_in_call(target_id, call_id, uid_str, target_id)

    # Relay through Redis pub/sub so other workers can deliver it
    await _relay(
        target_id,
        {
            "type": "call_offer",
            "call_id": call_id,
            "from": sender_name,
            "sdp": sdp,
        },
        queue_if_offline=False,
    )

    logger.info("Call offer relayed: %s -> %s (call_id=%s)", sender_name, to, call_id)


async def _handle_call_answer(
    websocket: WebSocket,
    msg: dict,
    uid_str: str,
    sender_name: str,
) -> None:
    call_id = msg.get("call_id")
    to = msg.get("to")
    sdp = msg.get("sdp")

    if not all([call_id, to, sdp]):
        await _send_error(websocket, "call_answer requires call_id, to, sdp")
        return

    target_id = await _resolve_username_to_id(to)
    if target_id is None:
        await _send_error(websocket, "Recipient not found")
        return

    await _relay(
        target_id,
        {
            "type": "call_answer",
            "call_id": call_id,
            "from": sender_name,
            "sdp": sdp,
        },
        queue_if_offline=False,
    )

    logger.info("Call answer relayed: %s -> %s (call_id=%s)", sender_name, to, call_id)


async def _handle_call_ice(
    websocket: WebSocket,
    msg: dict,
    uid_str: str,
    sender_name: str,
) -> None:
    call_id = msg.get("call_id")
    to = msg.get("to")
    candidate = msg.get("candidate")

    if not all([call_id, to, candidate]):
        await _send_error(
            websocket, "call_ice_candidate requires call_id, to, candidate"
        )
        return

    target_id = await _resolve_username_to_id(to)
    if target_id is None:
        return

    await _relay(
        target_id,
        {
            "type": "call_ice_candidate",
            "call_id": call_id,
            "from": sender_name,
            "candidate": candidate,
        },
        queue_if_offline=False,
    )


async def _handle_call_decline(
    websocket: WebSocket,
    msg: dict,
    uid_str: str,
    sender_name: str,
) -> None:
    call_id = msg.get("call_id")
    to = msg.get("to")

    if not all([call_id, to]):
        await _send_error(websocket, "call_decline requires call_id, to")
        return

    await _end_call_state(call_id)

    target_id = await _resolve_username_to_id(to)
    if target_id:
        await _relay(
            target_id,
            {
                "type": "call_decline",
                "call_id": call_id,
                "from": sender_name,
            },
            queue_if_offline=False,
        )

    logger.info("Call declined: %s -> %s (call_id=%s)", sender_name, to, call_id)


async def _handle_call_end(
    websocket: WebSocket,
    msg: dict,
    uid_str: str,
    sender_name: str,
) -> None:
    call_id = msg.get("call_id")
    to = msg.get("to")

    if not all([call_id, to]):
        await _send_error(websocket, "call_end requires call_id, to")
        return

    await _end_call_state(call_id)

    target_id = await _resolve_username_to_id(to)
    if target_id:
        await _relay(
            target_id,
            {
                "type": "call_end",
                "call_id": call_id,
                "from": sender_name,
            },
            queue_if_offline=False,
        )

    logger.info("Call ended: %s -> %s (call_id=%s)", sender_name, to, call_id)


# ─── Call signal dispatcher ───────────────────────────────────────────────────

_CALL_HANDLERS = {
    "call_offer": _handle_call_offer,
    "call_answer": _handle_call_answer,
    "call_ice_candidate": _handle_call_ice,
    "call_decline": _handle_call_decline,
    "call_end": _handle_call_end,
}

CALL_SIGNAL_TYPES = frozenset(_CALL_HANDLERS)


# ─── WebSocket endpoint ───────────────────────────────────────────────────────


@router.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket, ticket: str = Query(...)):
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

    # Mark online in Redis — visible to all workers
    await _set_presence(uid_str)

    # Tell every other connected client this user just came online
    if sender_name:
        await _broadcast_presence(sender_name, online=True)

    logger.info("WS connected: %s", sender_name)

    await _flush_offline_queue(uid_str, websocket)

    # Send this client a snapshot of who's currently online
    try:
        online_names = await _get_online_usernames()
        await websocket.send_text(
            json.dumps(
                {
                    "type": "presence_snapshot",
                    "online": online_names,
                }
            )
        )
    except Exception:
        logger.exception("Failed to send presence snapshot to %s", sender_name)

    try:
        while True:
            raw = await websocket.receive_text()

            if (
                len(raw) > MAX_CIPHERTEXT_LEN + 4096
            ):  # 4 KB headroom for envelope fields
                await _send_error(websocket, "Message too large")
                continue

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await _send_error(websocket, "Invalid JSON")
                continue

            msg_type = msg.get("type")

            if not msg_type:
                await _send_error(websocket, "Missing required field: type")
                continue

            if msg_type in ("chat_message", "voice"):
                await _handle_chat(websocket, msg, uid_str, sender_name)
            elif msg_type in CALL_SIGNAL_TYPES:
                handler = _CALL_HANDLERS[msg_type]
                await handler(websocket, msg, uid_str, sender_name)
            else:
                await _send_error(websocket, f"Unknown message type: {msg_type}")

    except WebSocketDisconnect:
        logger.info("WS disconnected: %s", sender_name)
    finally:
        if _connections.get(uid_str) is websocket:
            _connections.pop(uid_str, None)
        if sender_name:
            _online_users.pop(sender_name, None)

        # Remove Redis presence key and notify all clients
        await _clear_presence(uid_str)
        if sender_name:
            await _broadcast_presence(sender_name, online=False)

        # Synthesize call_end for the peer if this user was in an active call
        try:
            call_id = await _get_user_call(uid_str)
            if call_id:
                parts = await _get_call_participants(call_id)
                if parts:
                    caller_id, callee_id = parts
                    peer_id = callee_id if caller_id == uid_str else caller_id
                    await _end_call_state(call_id)
                    await _relay(
                        peer_id,
                        {
                            "type": "call_end",
                            "call_id": call_id,
                            "from": sender_name,
                        },
                        queue_if_offline=False,
                    )
                    logger.info(
                        "Synthesized call_end for peer %s on disconnect of %s",
                        peer_id,
                        sender_name,
                    )
        except Exception:
            logger.exception(
                "Error cleaning up call state on disconnect for %s", sender_name
            )


async def start_subscriber():
    global _subscriber_task
    _subscriber_task = asyncio.create_task(_redis_subscriber())


async def stop_subscriber():
    global _subscriber_task
    if _subscriber_task is not None:
        _subscriber_task.cancel()
        try:
            await _subscriber_task
        except asyncio.CancelledError:
            pass
        _subscriber_task = None
        logger.info("Redis subscriber stopped")
