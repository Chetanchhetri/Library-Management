import asyncio
import hmac
from dataclasses import dataclass
from datetime import timedelta

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .database import get_db
from .models import User, utcnow

ALGORITHM = "HS256"
bearer_scheme = HTTPBearer(auto_error=False)


# ---------- passwords (bcrypt is CPU-heavy -> thread pool) ----------
def _hash(pw: str) -> str:
    return bcrypt.hashpw(pw.encode()[:72], bcrypt.gensalt()).decode()


def _verify(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode()[:72], hashed.encode())
    except ValueError:
        return False


async def hash_password(pw: str) -> str:
    return await asyncio.to_thread(_hash, pw)


async def verify_password(pw: str, hashed: str) -> bool:
    return await asyncio.to_thread(_verify, pw, hashed)


def is_admin_credentials(email: str, password: str) -> bool:
    ok_email = hmac.compare_digest(email.strip().lower().encode(), settings.ADMIN_EMAIL.lower().encode())
    ok_pw = hmac.compare_digest(password.encode(), settings.ADMIN_PASSWORD.encode())
    return ok_email and ok_pw


# ---------- JWT ----------
def create_token(sub: str, role: str) -> str:
    exp = utcnow() + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    return jwt.encode({"sub": sub, "role": role, "exp": exp}, settings.SECRET_KEY, algorithm=ALGORITHM)


@dataclass
class Principal:
    role: str                 # "admin" | "student"
    user_id: int | None       # None for the admin
    email: str


async def principal_from_token(token: str, db: AsyncSession) -> Principal | None:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None
    role, sub = payload.get("role"), str(payload.get("sub", ""))
    if role == "admin" and sub.lower() == settings.ADMIN_EMAIL.lower():
        return Principal("admin", None, sub)
    if role == "student" and sub.isdigit():
        user = await db.get(User, int(sub))
        if user and user.is_verified:
            return Principal("student", user.id, user.email)
    return None


# ---------- FastAPI dependencies ----------
async def get_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> Principal:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated",
                            headers={"WWW-Authenticate": "Bearer"})
    principal = await principal_from_token(creds.credentials, db)
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token",
                            headers={"WWW-Authenticate": "Bearer"})
    return principal


async def require_student(p: Principal = Depends(get_principal)) -> Principal:
    if p.role != "student":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Student account required")
    return p


async def require_admin(p: Principal = Depends(get_principal)) -> Principal:
    if p.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    return p
