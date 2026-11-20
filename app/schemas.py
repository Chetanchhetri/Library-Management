from pydantic import BaseModel, EmailStr, Field


class SignupIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class VerifyOtpIn(BaseModel):
    email: str
    otp_code: str = Field(pattern=r"^\d{6}$")


class LoginIn(BaseModel):
    email: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str


class MessageOut(BaseModel):
    message: str


class BookIn(BaseModel):
    duration_minutes: int = Field(ge=30, le=180, description="30-180 minutes (max 3 hours)")


class CheckInIn(BaseModel):
    qr_token: str = Field(min_length=8, description="Token printed in the seat's QR code")


class RenewIn(BaseModel):
    additional_duration_minutes: int = Field(ge=30, le=180)
