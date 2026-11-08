import re
import time
import uuid
from collections import defaultdict
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.core.errors import PermissionDenied, ValidationFailed
from app.core.security import create_access_token, create_refresh_token, decode_token, hash_secret, verify_secret
from app.core.config import settings
from app.db.session import get_db
from app.models.identity import RefreshToken, User
from app.models.platform import SignupRequest
from app.models.tenant import Tenant
from app.schemas.common import Strict
from app.services import context as context_service
from app.services import signups as signup_service
from app.services.common import utcnow

router = APIRouter(prefix="/auth", tags=["Auth"])

_FAILS: dict[str, list[float]] = defaultdict(list)
_SIGNUPS: dict[str, list[float]] = defaultdict(list)
# Per-process brake; put a shared limiter in front for a multi-instance deploy. Failures are counted per
# (phone, client address) so a stranger cannot lock a shop out, plus a much higher ceiling per phone alone
# that still stops a brute force spread over many addresses.
_WINDOW, _MAX_FAILS, _MAX_FAILS_PER_PHONE = 600, 8, 60
REFRESH_GRACE_SECONDS = 30  # a rotated refresh token still works briefly, so a lost response or a second tab is not a logout


def normalise_phone(raw: str) -> str:
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits.startswith("880") and len(digits) == 13:
        digits = "0" + digits[3:]
    return digits


class LoginIn(Strict):
    phone: str = Field(min_length=6, max_length=20)
    password: str = Field(min_length=1, max_length=72)


class SignupIn(Strict):
    phone: str = Field(min_length=6, max_length=20)
    password: str = Field(min_length=8, max_length=72)
    business_name: str = Field(min_length=2, max_length=160)
    business_type_key: str = Field(min_length=1, max_length=40)
    language: str = Field(default="bn", pattern="^(en|bn)$")


class SignupStatusIn(Strict):
    phone: str = Field(min_length=6, max_length=20)
    password: str = Field(min_length=1, max_length=72)


class RefreshIn(Strict):
    refresh_token: str


class ChangePasswordIn(Strict):
    current_password: str
    new_password: str = Field(min_length=8, max_length=72)


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return hash_secret("no-such-account")


async def revoke_sessions(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Sign a user out everywhere: every refresh token they hold stops working (access tokens lapse on their own)."""
    await db.execute(update(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.revoked.is_(False)).values(revoked=True))


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
async def login(body: LoginIn, request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    phone = normalise_phone(body.phone)
    pair = f"{phone}|{request.client.host if request.client else ''}"
    now = time.time()
    for key in (pair, phone):
        _FAILS[key] = [t for t in _FAILS[key] if now - t < _WINDOW]
    if len(_FAILS[pair]) >= _MAX_FAILS or len(_FAILS[phone]) >= _MAX_FAILS_PER_PHONE:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Try again in a few minutes.")
    user = (await db.execute(select(User).where(User.phone == phone))).scalar_one_or_none()
    # Always spend a bcrypt check, so an unknown phone takes as long as a wrong password.
    password_ok = verify_secret(body.password, user.password_hash if user is not None else _dummy_hash())
    if user is None:
        waiting = await _signup_for(db, phone, body.password)
        if waiting is not None and waiting.status == "pending":
            raise PermissionDenied("Your request is still being reviewed", code="signup_pending")
        if waiting is not None and waiting.status == "rejected":
            raise PermissionDenied(waiting.reject_reason or "Your request was not approved", code="signup_rejected")
    if user is None or not user.active or not password_ok:
        _FAILS[pair].append(now)
        _FAILS[phone].append(now)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong phone number or password")
    _FAILS.pop(pair, None)
    user.last_login_at = utcnow()
    tokens = await _issue(db, user)
    payload = await session_payload(db, user, tokens)
    await db.commit()
    return payload


async def _signup_for(db: AsyncSession, phone: str, password: str) -> SignupRequest | None:
    """The newest signup request for this phone, but only if the password matches it (so status never leaks to a stranger)."""
    req = (await db.execute(select(SignupRequest).where(SignupRequest.phone == phone).order_by(SignupRequest.created_at.desc()).limit(1))).scalar_one_or_none()
    return req if req is not None and verify_secret(password, req.password_hash) else None


def _brake(request: Request, key: str, limit: int, window: int) -> None:
    now = time.time()
    pair = f"{key}|{request.client.host if request.client else ''}"
    _SIGNUPS[pair] = [t for t in _SIGNUPS[pair] if now - t < window]
    if len(_SIGNUPS[pair]) >= limit:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Try again in a few minutes.")
    _SIGNUPS[pair].append(now)


@router.post("/signup", status_code=201, summary="Ask to open a business — waits for a super admin to approve")
async def signup(body: SignupIn, request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    _brake(request, "signup", 10, 3600)
    phone = normalise_phone(body.phone)
    if not re.fullmatch(r"01[3-9]\d{8}", phone):
        raise ValidationFailed("Enter a Bangladesh mobile number like 01712345678")
    req = await signup_service.create_request(db, phone=phone, password=body.password, business_name=body.business_name,
                                              business_type_key=body.business_type_key, language=body.language)
    await db.commit()
    return signup_service.request_out(req)


@router.post("/signup/status", summary="Where is my request? (phone + the password chosen at signup)")
async def signup_status(body: SignupStatusIn, request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    phone = normalise_phone(body.phone)
    _brake(request, f"signup-status:{phone}", 120, 600)
    req = await _signup_for(db, phone, body.password)
    if req is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No request found for that number and password")
    from app.models.platform import BusinessType

    bt = (await db.execute(select(BusinessType).where(BusinessType.key == req.business_type_key))).scalar_one_or_none()
    return signup_service.request_out(req, business_type_label=bt.label if bt else None)


@router.post("/refresh", summary="Exchange a refresh token for a new pair (rotates)")
async def refresh(body: RefreshIn, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        claims = decode_token(body.refresh_token)
    except ValueError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired — sign in again") from exc
    if claims.get("type") != "refresh":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not a refresh token")
    row = (await db.execute(select(RefreshToken).where(RefreshToken.jti == claims["jti"]).with_for_update())).scalar_one_or_none()
    now = utcnow()
    if row is None or row.expires_at < now:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired — sign in again")
    recently_rotated = row.revoked and row.rotated_at is not None and (now - row.rotated_at).total_seconds() <= REFRESH_GRACE_SECONDS
    if row.revoked and not recently_rotated:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired — sign in again")
    user = await db.get(User, row.user_id)
    if user is None or not user.active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account disabled")
    if not row.revoked:
        row.revoked, row.rotated_at = True, now
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
    await revoke_sessions(db, user.id)
    await db.commit()
