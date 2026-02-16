# Whisper Server

End-to-end encrypted chat backend built with FastAPI, PostgreSQL, and Redis.

The server never sees plaintext messages — it only relays encrypted payloads between clients using NaCl (Curve25519) public-key cryptography.

## Architecture

```
Client A ──► REST API (auth, keys) ──► PostgreSQL (users, keys)
    │                                        │
    └──► WebSocket ◄── Redis Pub/Sub ──► WebSocket ──► Client B
              (encrypted messages relay)
```

- **FastAPI** — async REST + WebSocket framework
- **PostgreSQL** — user accounts and public keys
- **Redis** — sessions, rate limiting, cross-instance message routing via pub/sub
- **Gunicorn + Uvicorn** — production ASGI server with multiple workers
- **Alembic** — database migrations

## Quick Start

### Prerequisites

- Docker and Docker Compose

### Setup

```bash
# Clone and enter the directory
cd server

# Copy environment template and configure
cp .env.example .env
# Edit .env — at minimum, set a strong JWT_SECRET_KEY:
# python -c "import secrets; print(secrets.token_hex(32))"

# Start all services
docker compose up -d

# Run database migrations
docker compose exec api alembic upgrade head
```

The API is now available at `http://localhost:8000`.

### Local Development (without Docker)

```bash
# Create a virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Start PostgreSQL and Redis (via Docker or locally)
docker compose up -d postgres redis

# Run migrations
alembic upgrade head

# Start the dev server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Set `ENVIRONMENT=development` in `.env` to enable the interactive docs at `/docs`.

## API Reference

### Authentication

| Method | Endpoint          | Auth | Description                       |
| ------ | ----------------- | ---- | --------------------------------- |
| `POST` | `/auth/register`  | No   | Create a new account              |
| `POST` | `/auth/login`     | No   | Get an access token               |
| `POST` | `/auth/logout`    | Yes  | Invalidate current session        |
| `POST` | `/auth/ws-ticket` | Yes  | Get a single-use WebSocket ticket |

### Users

| Method | Endpoint                       | Auth | Description                            |
| ------ | ------------------------------ | ---- | -------------------------------------- |
| `PUT`  | `/users/me/public-key`         | Yes  | Update your public key                 |
| `GET`  | `/users/{username}/public-key` | Yes  | Get a user's public key                |
| `GET`  | `/users/{username}/exists`     | Yes  | Check if a user exists + online status |

### WebSocket

| Endpoint                      | Description                       |
| ----------------------------- | --------------------------------- |
| `WS /ws/chat?ticket=<ticket>` | Real-time encrypted message relay |

### Health

| Method | Endpoint  | Description          |
| ------ | --------- | -------------------- |
| `GET`  | `/health` | Service health check |

### Authentication Flow

```
1. POST /auth/register  →  { username, password, public_key }
2. POST /auth/login     →  { access_token, token_type }
3. POST /auth/ws-ticket →  { ticket }
4. WS   /ws/chat?ticket=<ticket>
```

All authenticated endpoints require `Authorization: Bearer <access_token>`.

### WebSocket Message Format

**Send:**

```json
{
  "to": "recipient_username",
  "ciphertext": "<base64-encoded encrypted message>",
  "nonce": "<base64-encoded nonce>"
}
```

**Receive:**

```json
{
  "from": "sender_username",
  "ciphertext": "<base64-encoded encrypted message>",
  "nonce": "<base64-encoded nonce>",
  "timestamp": 1700000000
}
```

**Delivery status (sent back to sender):**

```json
{
  "type": "status",
  "to": "recipient_username",
  "delivered": true,
  "timestamp": 1700000000
}
```

## Database Migrations

```bash
# Apply all pending migrations
alembic upgrade head

# Generate a new migration after changing models
alembic revision --autogenerate -m "describe the change"

# Rollback one migration
alembic downgrade -1
```

## Environment Variables

| Variable            | Description                          | Default      |
| ------------------- | ------------------------------------ | ------------ |
| `POSTGRES_USER`     | PostgreSQL username                  | `whisper`    |
| `POSTGRES_PASSWORD` | PostgreSQL password                  | -            |
| `POSTGRES_DB`       | PostgreSQL database name             | `whisper`    |
| `DATABASE_URL`      | Full async database URL              | -            |
| `REDIS_PASSWORD`    | Redis password                       | -            |
| `REDIS_URL`         | Full Redis URL (includes password)   | -            |
| `JWT_SECRET_KEY`    | Secret for signing JWTs              | -            |
| `JWT_ALGORITHM`     | JWT signing algorithm                | `HS256`      |
| `JWT_EXPIRE_DAYS`   | Token expiration in days             | `7`          |
| `LOGIN_RATE_LIMIT`  | Max login attempts per window        | `5`          |
| `LOGIN_RATE_WINDOW` | Rate limit window in seconds         | `300`        |
| `CORS_ORIGINS`      | Comma-separated allowed origins      | `""`         |
| `ENVIRONMENT`       | `development` or `production`        | `production` |
| `WS_TICKET_TTL`     | WebSocket ticket lifetime in seconds | `30`         |

## Security

- Passwords hashed with **bcrypt**
- JWT tokens with Redis-backed session validation
- **Constant-time** authentication checks to prevent timing attacks
- IP-based **rate limiting** on login and registration (atomic Lua scripts)
- WebSocket auth via **single-use tickets** (avoids JWTs in query strings)
- Public keys validated as **32-byte NaCl Curve25519** keys
- Payload size limits on ciphertext (65KB) and nonce (64B)
- Non-root Docker container
- Swagger docs **disabled in production**

## Project Structure

```
server/
├── alembic/                 # Database migrations
│   ├── versions/            # Migration scripts
│   ├── env.py               # Alembic async config
│   └── script.py.mako       # Migration template
├── app/
│   ├── middleware/
│   │   └── auth.py          # Sessions, WS tickets, rate limiting
│   ├── models/
│   │   └── user.py          # SQLAlchemy User model
│   ├── routers/
│   │   ├── auth.py          # Auth endpoints
│   │   └── users.py         # User endpoints
│   ├── schemas/
│   │   └── auth.py          # Pydantic request/response models
│   ├── security/
│   │   ├── jwt.py           # JWT creation and validation
│   │   └── password.py      # Bcrypt hashing
│   ├── ws/
│   │   └── chat.py          # WebSocket handler + Redis pub/sub
│   ├── config.py            # Settings from .env
│   ├── database.py          # SQLAlchemy async engine
│   ├── main.py              # FastAPI app setup
│   └── redis.py             # Redis client
├── .env.example             # Environment template
├── .gitignore
├── .dockerignore
├── alembic.ini
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```

## License

[MIT](./LICENSE)
