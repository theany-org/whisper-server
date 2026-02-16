from passlib.context import CryptContext

_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Precomputed bcrypt hash of "dummy" — used for constant-time login checks
# when the username doesn't exist, avoiding a double-hash per attempt.
DUMMY_HASH: str = _ctx.hash("dummy")


def hash_password(plain: str) -> str:
    return _ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _ctx.verify(plain, hashed)
