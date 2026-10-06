import time
import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.core.errors import ValidationFailed
from app.core.security import create_access_token, create_refresh_token, decode_token, hash_secret, verify_secret
from app.core.config import settings
from app.db.session import get_db
from app.models.identity import RefreshToken, User
from app.models.tenant import Tenant
from app.schemas.common import Strict
from app.services import context as context_service
from app.services.common import utcnow

router = APIRouter(prefix="/auth", tags=["Auth"])

_FAILS: dict[str, list[float]] = defaultdict(list)
_WINDOW, _MAX_FAILS = 600, 8  # per-process brake; put a shared limiter in front for a multi-instance deploy


def normalise_phone(raw: str) -> str:
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits.startswith("880") and len(digits) == 13:
        digits = "0" + digits[3:]
    return digits


class LoginIn(Strict):
    phone: str = Field(min_length=6, max_length=20)
    password: str = Field(min_length=1, max_length=72)


class RefreshIn(Strict):
    refresh_token: str


class ChangePasswordIn(Strict):
    current_password: str
    new_password: str = Field(min_length=8, max_length=72)


async def _issue(db: AsyncSession, user: User, impersonated_by: uuid.UUID | None = None) -> dict:
    access = create_access_token(user_id=user.id, tenant_id=user.tenant_id, role=user.role, impersonated_by=impersonated_by)
    refresh, jti, expires = create_refresh_token(user_id=user.id)
    db.add(RefreshToken(user_id=user.id, jti=jti, expires_at=expires))
    return {
        "access_token": access, "refresh_token": refresh, "token_type": "bearer",
        "expires_in": settings.access_token_expire_minutes * 60,
    }


async def session_payload(db: AsyncSession, user: User, tokens: dict) -> dict:
    out = {**tokens, "user": context_service.user_out(user), "context": None}
    if user.tenant_id:
        tenant = await db.get(Tenant, user.tenant_id)
        out["context"] = await context_service.build_context(db, tenant, user)
    return out


@router.post("/login", summary="Sign in with phone + password")
async def login(body: LoginIn, db: AsyncSession = Depends(get_db)) -> dict:
    phone = normalise_phone(body.phone)
    now = time.time()
    _FAILS[phone] = [t for t in _FAILS[phone] if now - t < _WINDOW]
    if len(_FAILS[phone]) >= _MAX_FAILS:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Try again in a few minutes.")
    user = (await db.execute(select(User).where(User.phone == phone))).scalar_one_or_none()
    if user is None or not user.active or not verify_secret(body.password, user.password_hash):
        _FAILS[phone].append(now)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong phone number or password")
    _FAILS.pop(phone, None)
    user.last_login_at = utcnow()
    tokens = await _issue(db, user)
    payload = await session_payload(db, user, tokens)
    await db.commit()
    return payload


@router.post("/refresh", summary="Exchange a refresh token for a new pair (rotates)")
async def refresh(body: RefreshIn, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        claims = decode_token(body.refresh_token)
    except ValueError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired — sign in again") from exc
    if claims.get("type") != "refresh":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not a refresh token")
    row = (await db.execute(select(RefreshToken).where(RefreshToken.jti == claims["jti"]).with_for_update())).scalar_one_or_none()
    if row is None or row.revoked or row.expires_at < utcnow():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired — sign in again")
    user = await db.get(User, row.user_id)
    if user is None or not user.active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account disabled")
    row.revoked = True
    tokens = await _issue(db, user)
    await db.commit()
    return {**tokens, "user": context_service.user_out(user)}


@router.post("/logout", status_code=204, summary="Revoke a refresh token")
async def logout(body: RefreshIn, db: AsyncSession = Depends(get_db)) -> None:
    try:
        claims = decode_token(body.refresh_token)
    except ValueError:
        return
    row = (await db.execute(select(RefreshToken).where(RefreshToken.jti == claims.get("jti")))).scalar_one_or_none()
    if row:
        row.revoked = True
        await db.commit()


@router.get("/me", summary="The signed-in user (and their business context)")
async def me(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict:
    out = {"user": context_service.user_out(user), "context": None}
    if user.tenant_id:
        out["context"] = await context_service.build_context(db, await db.get(Tenant, user.tenant_id), user)
    return out


@router.post("/change-password", status_code=204)
async def change_password(body: ChangePasswordIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> None:
    if not verify_secret(body.current_password, user.password_hash):
        raise ValidationFailed("Current password is wrong", code="wrong_password")
    user.password_hash = hash_secret(body.new_password)
    await db.commit()
