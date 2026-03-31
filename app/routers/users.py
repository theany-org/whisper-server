import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user_id
from app.models.user import User
from app.redis import get_redis
from app.schemas.auth import PublicKeyResponse, _validate_nacl_public_key
from app.ws.chat import _is_online

router = APIRouter(prefix="/users", tags=["users"])


class UpdatePublicKeyRequest(BaseModel):
    public_key: str

    @field_validator("public_key")
    @classmethod
    def validate_public_key(cls, v: str) -> str:
        return _validate_nacl_public_key(v)


@router.put("/me/public-key", status_code=status.HTTP_200_OK)
async def update_public_key(
    body: UpdatePublicKeyRequest,
    current_user_id: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == current_user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    user.public_key = body.public_key
    await db.commit()

    # Any messages queued while this user was offline were encrypted for the
    # old public key and cannot be decrypted after a key rotation. Clear the
    # queue so stale ciphertext is not delivered on the next connection.
    r = get_redis()
    try:
        await r.delete(f"offline:{current_user_id}")
    finally:
        await r.aclose()

    return {"message": "Public key updated"}


@router.get("/{username}/public-key", response_model=PublicKeyResponse)
async def get_public_key(
    username: str,
    _current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(User.username, User.public_key).where(User.username == username.lower())
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return PublicKeyResponse(username=row.username, public_key=row.public_key)


@router.get("/{username}/exists")
async def check_user_exists(
    username: str,
    _current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(User.id, User.username).where(User.username == username.lower())
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return {"username": row.username, "online": await _is_online(str(row.id))}
