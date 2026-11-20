import hmac
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import create_token, hash_password, is_admin_credentials, verify_password
from ..config import settings
from ..database import get_db, write_tx
from ..email_service import EmailError, send_otp_email
from ..models import User, utcnow
from ..schemas import LoginIn, MessageOut, SignupIn, TokenOut, VerifyOtpIn

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/signup", response_model=MessageOut, status_code=201)
async def signup(body: SignupIn):
    email = body.email.strip().lower()
    if email == settings.ADMIN_EMAIL.strip().lower():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This email cannot be registered")

    pw_hash = await hash_password(body.password)
    otp = f"{secrets.randbelow(10**6):06d}"
    expires = utcnow() + timedelta(minutes=settings.OTP_VALID_MINUTES)

    async with write_tx() as db:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user and user.is_verified:
            raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
        if user:   # unverified re-signup: refresh details + OTP
            user.name, user.password_hash = body.name.strip(), pw_hash
            user.otp_code, user.otp_expires_at = otp, expires
        else:
            db.add(User(name=body.name.strip(), email=email, password_hash=pw_hash,
                        otp_code=otp, otp_expires_at=expires, is_verified=False))

    try:
        await send_otp_email(email, body.name.strip(), otp)
    except EmailError:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            "Account created but the OTP email could not be sent. Try signing up again.")
    return MessageOut(message=f"OTP sent to {email}. It is valid for {settings.OTP_VALID_MINUTES} minutes.")


@router.post("/verify-otp", response_model=MessageOut)
async def verify_otp(body: VerifyOtpIn):
    email = body.email.strip().lower()
    async with write_tx() as db:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if not user:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid email or OTP")
        if user.is_verified:
            return MessageOut(message="Already verified")
        if (not user.otp_code or not user.otp_expires_at or user.otp_expires_at < utcnow()
                or not hmac.compare_digest(user.otp_code, body.otp_code)):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired OTP")
        user.is_verified = True
        user.otp_code = None
        user.otp_expires_at = None
    return MessageOut(message="Email verified. You can now log in.")


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, db: AsyncSession = Depends(get_db)):
    email = body.email.strip().lower()

    # 1) Admin credentials come from .env only
    if email == settings.ADMIN_EMAIL.strip().lower():
        if is_admin_credentials(email, body.password):
            return TokenOut(access_token=create_token(settings.ADMIN_EMAIL, "admin"), role="admin")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")

    # 2) Verified students
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if not user or not await verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    if not user.is_verified:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Email not verified")
    return TokenOut(access_token=create_token(str(user.id), "student"), role="student")
