import base64
import re

from pydantic import BaseModel, field_validator


_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,32}$")

# NaCl Curve25519 public key: exactly 32 bytes
_NACL_PUBLIC_KEY_BYTES = 32


def _validate_nacl_public_key(v: str) -> str:
    """Validate that v is a base64-encoded 32-byte NaCl public key."""
    stripped = v.strip()
    if not stripped:
        raise ValueError("Public key must not be empty")
    try:
        raw = base64.b64decode(stripped, validate=True)
    except Exception:
        raise ValueError("Public key must be valid base64")
    if len(raw) != _NACL_PUBLIC_KEY_BYTES:
        raise ValueError(
            f"Public key must be exactly {_NACL_PUBLIC_KEY_BYTES} bytes"
        )
    return stripped


class RegisterRequest(BaseModel):
    username: str
    password: str
    public_key: str

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        if not _USERNAME_RE.match(v):
            raise ValueError(
                "Username must be 3-32 characters: letters, digits, underscores only"
            )
        return v.lower()

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        if len(v) > 128:
            raise ValueError("Password must not exceed 128 characters")
        return v

    @field_validator("public_key")
    @classmethod
    def validate_public_key(cls, v: str) -> str:
        return _validate_nacl_public_key(v)


class LoginRequest(BaseModel):
    username: str
    password: str

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        if not _USERNAME_RE.match(v):
            raise ValueError("Invalid credentials")
        return v.lower()


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MessageResponse(BaseModel):
    message: str


class PublicKeyResponse(BaseModel):
    username: str
    public_key: str
