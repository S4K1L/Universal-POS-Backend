import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from app.core.config import settings


def hash_secret(plain: str) -> str:
    return bcrypt.hashpw(plain.encode()[:72], bcrypt.gensalt(rounds=12)).decode()


def verify_secret(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode()[:72], hashed.encode())
    except ValueError:
        return False


def _encode(claims: dict[str, Any], ttl: timedelta) -> str:
    now = datetime.now(timezone.utc)
    payload = {"iat": now, "exp": now + ttl, "jti": str(uuid.uuid4()), **claims}
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_access_token(
    *, user_id: uuid.UUID, tenant_id: uuid.UUID | None, role: str, impersonated_by: uuid.UUID | None = None,
    ttl: timedelta | None = None,
) -> str:
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "type": "access",
        "tenant_id": str(tenant_id) if tenant_id else None,
        "role": role,
    }
    if impersonated_by:
        claims["impersonated_by"] = str(impersonated_by)
    return _encode(claims, ttl or timedelta(minutes=settings.access_token_expire_minutes))


def create_refresh_token(*, user_id: uuid.UUID) -> tuple[str, str, datetime]:
    ttl = timedelta(days=settings.refresh_token_expire_days)
    jti = str(uuid.uuid4())
    token = _encode({"sub": str(user_id), "type": "refresh", "jti": jti}, ttl)
    return token, jti, datetime.now(timezone.utc) + ttl


def decode_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise ValueError("Invalid or expired token") from exc
