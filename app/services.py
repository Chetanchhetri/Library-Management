"""Shared helpers: audit logging, seat state transitions, public seat views, seeding."""
import hashlib
import hmac
import json
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .database import write_tx
from .models import AuditLog, Booking, Seat, utcnow

ACTIVE = ("PENDING_CHECKIN", "OCCUPIED")


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def make_qr_token(seat_id: int) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), str(seat_id).encode(), hashlib.sha256).hexdigest()


def log_event(db: AsyncSession, action: str, *, user_id=None, seat_id=None, booking_id=None, details=None):
    db.add(AuditLog(user_id=user_id, seat_id=seat_id, booking_id=booking_id, action_type=action,
                    timestamp=utcnow(), details_json=json.dumps(details or {})))


def finish_booking(seat: Seat, booking: Booking, new_status: str, ended_at: datetime) -> None:
    """Close a booking and free its seat (caller must be inside write_tx)."""
    booking.status = new_status
    booking.ended_at = ended_at
    if booking.checked_in_at:
        booking.actual_duration_mins = max(0, round((ended_at - booking.checked_in_at).total_seconds() / 60))
    else:
        booking.actual_duration_mins = 0
    seat.status = "AVAILABLE"
    seat.current_booking_id = None


def seat_view(seat: Seat, booking: Booking | None, now: datetime, include_user: bool = False) -> dict:
    expires = None
    if booking is not None:
        if seat.status == "PENDING_CHECKIN":
            expires = booking.checkin_deadline
        elif seat.status == "OCCUPIED":
            expires = booking.session_ends_at
    view = {
        "id": seat.id,
        "seat_code": seat.seat_code,
        "floor": seat.floor,
        "zone": seat.zone,
        "status": seat.status,
        "remaining_seconds": max(0, int((expires - now).total_seconds())) if expires else None,
        "expires_at": iso(expires),
    }
    if include_user and booking is not None:   # admin only
        view["user_id"] = booking.user_id
        view["booking_id"] = booking.id
    return view


async def load_seat_views(db: AsyncSession, seat_ids=None, include_user: bool = False) -> list[dict]:
    stmt = (select(Seat, Booking)
            .outerjoin(Booking, Booking.id == Seat.current_booking_id)
            .order_by(Seat.floor, Seat.seat_code))
    if seat_ids:
        stmt = stmt.where(Seat.id.in_(list(seat_ids)))
    now = utcnow()
    rows = (await db.execute(stmt)).all()
    return [seat_view(s, b, now, include_user) for s, b in rows]


def group_by_floor(views: list[dict]) -> dict[str, list[dict]]:
    floors: dict[str, list[dict]] = {"1": [], "2": [], "3": []}
    for v in views:
        floors.setdefault(str(v["floor"]), []).append(v)
    return floors


def booking_out(b: Booking, seat_code: str | None = None) -> dict:
    return {
        "booking_id": b.id, "seat_id": b.seat_id, "seat_code": seat_code, "status": b.status,
        "booked_duration_mins": b.booked_duration_mins, "actual_duration_mins": b.actual_duration_mins,
        "renewal_count": b.renewal_count, "created_at": iso(b.created_at),
        "checkin_deadline": iso(b.checkin_deadline), "checked_in_at": iso(b.checked_in_at),
        "session_ends_at": iso(b.session_ends_at), "ended_at": iso(b.ended_at),
    }


async def seed_seats() -> None:
    """3 floors x 10 seats = 30 seats (F1-A01..F1-A10, F2-B01.., F3-C01..F3-C10) if table is empty."""
    async with write_tx() as db:
        count = (await db.execute(select(func.count()).select_from(Seat))).scalar_one()
        if count:
            return
        for floor, zone in ((1, "A"), (2, "B"), (3, "C")):
            for n in range(1, 11):
                seat = Seat(seat_code=f"F{floor}-{zone}{n:02d}", floor=floor, zone=zone,
                            status="AVAILABLE", qr_secret_token="pending")
                db.add(seat)
                await db.flush()
                seat.qr_secret_token = make_qr_token(seat.id)
