from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def utcnow() -> datetime:
    """Naive UTC 'now' (SQLite stores naive datetimes; everything here is UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    otp_code: Mapped[str | None] = mapped_column(String(6), nullable=True)
    otp_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Seat(Base):
    __tablename__ = "seats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    seat_code: Mapped[str] = mapped_column(String(16), unique=True, index=True)  # e.g. F1-A01
    floor: Mapped[int] = mapped_column(Integer, index=True)
    zone: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(20), default="AVAILABLE")  # AVAILABLE|PENDING_CHECKIN|OCCUPIED
    current_booking_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    qr_secret_token: Mapped[str] = mapped_column(String(64))


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    seat_id: Mapped[int] = mapped_column(Integer, index=True)
    # PENDING_CHECKIN | OCCUPIED | NO_SHOW_TIMEOUT | SESSION_COMPLETED | EARLY_VACATED
    status: Mapped[str] = mapped_column(String(20), index=True)
    booked_duration_mins: Mapped[int] = mapped_column(Integer)
    actual_duration_mins: Mapped[int | None] = mapped_column(Integer, nullable=True)
    renewal_count: Mapped[int] = mapped_column(Integer, default=0)
    renewal_notified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    checkin_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    session_ends_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        # Database-level safety nets: at most one live booking per student and per seat.
        Index("uq_one_active_booking_per_user", "user_id", unique=True,
              sqlite_where=text("status IN ('PENDING_CHECKIN','OCCUPIED')")),
        Index("uq_one_active_booking_per_seat", "seat_id", unique=True,
              sqlite_where=text("status IN ('PENDING_CHECKIN','OCCUPIED')")),
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    seat_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    booking_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action_type: Mapped[str] = mapped_column(String(32), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
