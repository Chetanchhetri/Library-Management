import hmac
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import Principal, get_principal, principal_from_token, require_student
from ..config import settings
from ..database import SessionLocal, get_db, write_tx
from ..models import Booking, Seat, utcnow
from ..realtime import broadcast_seats, manager, snapshot_message
from ..schemas import BookIn, CheckInIn, RenewIn
from ..services import (ACTIVE, booking_out, finish_booking, group_by_floor, iso, load_seat_views,
                        log_event)

router = APIRouter(prefix="/api/seats", tags=["seats"])
users_router = APIRouter(prefix="/api/users/me", tags=["me"])
ws_router = APIRouter()


def _busy() -> HTTPException:
    return HTTPException(503, "Database busy, please retry")


async def _own_booking(db: AsyncSession, seat: Seat, user: Principal) -> Booking:
    booking = await db.get(Booking, seat.current_booking_id) if seat.current_booking_id else None
    if booking is None or booking.user_id != user.user_id:
        raise HTTPException(403, "This seat is not booked by you")
    return booking


# ------------------------------------------------------------------ list
@router.get("", summary="All seats grouped by floor, with status and remaining time")
async def list_seats(p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    views = await load_seat_views(db, include_user=(p.role == "admin"))
    return {"server_time": iso(utcnow()), "floors": group_by_floor(views)}


# ------------------------------------------------------------------ book
@router.post("/{seat_id}/book", status_code=201, summary="Select a seat and slot duration")
async def book_seat(seat_id: int, body: BookIn, user: Principal = Depends(require_student)):
    now = utcnow()
    try:
        async with write_tx() as db:                       # BEGIN IMMEDIATE
            seat = await db.get(Seat, seat_id)
            if seat is None:
                raise HTTPException(404, "Seat not found")
            mine = (await db.execute(select(Booking.id).where(
                Booking.user_id == user.user_id, Booking.status.in_(ACTIVE)))).first()
            if mine:
                raise HTTPException(409, "You already have an active booking")
            if seat.status != "AVAILABLE":
                raise HTTPException(409, "Seat taken")
            booking = Booking(
                user_id=user.user_id, seat_id=seat.id, status="PENDING_CHECKIN",
                booked_duration_mins=body.duration_minutes, renewal_count=0, renewal_notified=False,
                created_at=now, checkin_deadline=now + timedelta(minutes=settings.CHECKIN_WINDOW_MINUTES))
            db.add(booking)
            await db.flush()
            seat.status = "PENDING_CHECKIN"
            seat.current_booking_id = booking.id
            log_event(db, "BOOKED", user_id=user.user_id, seat_id=seat.id, booking_id=booking.id,
                      details={"duration_minutes": body.duration_minutes})
            code = seat.seat_code
    except IntegrityError:
        raise HTTPException(409, "Seat taken or you already have an active booking")
    except OperationalError:
        raise _busy()
    await broadcast_seats([seat_id])
    return booking_out(booking, code)


# ------------------------------------------------------------------ check-in
@router.post("/{seat_id}/checkin", summary="Physical QR check-in")
async def checkin(seat_id: int, body: CheckInIn, user: Principal = Depends(require_student)):
    now = utcnow()
    token = body.qr_token.strip().rsplit("/", 1)[-1]       # accepts raw token or libseat://<id>/<token>
    try:
        async with write_tx() as db:
            seat = await db.get(Seat, seat_id)
            if seat is None:
                raise HTTPException(404, "Seat not found")
            booking = await _own_booking(db, seat, user)
            if booking.status != "PENDING_CHECKIN" or seat.status != "PENDING_CHECKIN":
                raise HTTPException(409, "Booking is not awaiting check-in")
            if now > booking.checkin_deadline:
                raise HTTPException(410, "Check-in window expired")
            if not hmac.compare_digest(token.encode(), seat.qr_secret_token.encode()):
                raise HTTPException(403, "Invalid QR code for this seat")
            booking.status = "OCCUPIED"
            booking.checked_in_at = now
            booking.session_ends_at = now + timedelta(minutes=booking.booked_duration_mins)
            booking.renewal_notified = False
            seat.status = "OCCUPIED"
            log_event(db, "CHECKED_IN", user_id=user.user_id, seat_id=seat.id, booking_id=booking.id,
                      details={"session_ends_at": iso(booking.session_ends_at)})
            code = seat.seat_code
    except OperationalError:
        raise _busy()
    await broadcast_seats([seat_id])
    return booking_out(booking, code)


# ------------------------------------------------------------------ renew
@router.post("/{seat_id}/renew", summary="Extend the session (only in the final 5 minutes)")
async def renew(seat_id: int, body: RenewIn, user: Principal = Depends(require_student)):
    now = utcnow()
    try:
        async with write_tx() as db:
            seat = await db.get(Seat, seat_id)
            if seat is None:
                raise HTTPException(404, "Seat not found")
            booking = await _own_booking(db, seat, user)
            if booking.status != "OCCUPIED" or seat.status != "OCCUPIED":
                raise HTTPException(409, "Only an occupied (checked-in) session can be renewed")
            remaining = (booking.session_ends_at - now).total_seconds()
            if not (0 < remaining <= settings.RENEWAL_WINDOW_MINUTES * 60):
                raise HTTPException(409, f"Renewal opens in the last {settings.RENEWAL_WINDOW_MINUTES} "
                                         f"minutes of a session ({max(0, int(remaining))}s remaining)")
            previous_end = booking.session_ends_at
            booking.session_ends_at = now + timedelta(minutes=body.additional_duration_minutes)
            booking.renewal_count += 1
            booking.renewal_notified = False              # next window will notify again
            log_event(db, "RENEWED", user_id=user.user_id, seat_id=seat.id, booking_id=booking.id,
                      details={"additional_minutes": body.additional_duration_minutes,
                               "previous_end": iso(previous_end), "new_end": iso(booking.session_ends_at),
                               "renewal_count": booking.renewal_count})
            code = seat.seat_code
    except OperationalError:
        raise _busy()
    await broadcast_seats([seat_id])
    return booking_out(booking, code)


# ------------------------------------------------------------------ vacate
@router.post("/{seat_id}/vacate", summary="Leave early / cancel")
async def vacate(seat_id: int, user: Principal = Depends(require_student)):
    now = utcnow()
    try:
        async with write_tx() as db:
            seat = await db.get(Seat, seat_id)
            if seat is None:
                raise HTTPException(404, "Seat not found")
            booking = await _own_booking(db, seat, user)
            if booking.status not in ACTIVE:
                raise HTTPException(409, "No active booking on this seat")
            was_checked_in = booking.checked_in_at is not None
            finish_booking(seat, booking, "EARLY_VACATED", now)
            log_event(db, "EARLY_VACATE", user_id=user.user_id, seat_id=seat.id, booking_id=booking.id,
                      details={"actual_duration_mins": booking.actual_duration_mins,
                               "before_checkin": not was_checked_in})
            code = seat.seat_code
    except OperationalError:
        raise _busy()
    await broadcast_seats([seat_id])
    return booking_out(booking, code)


# ------------------------------------------------------------------ "me"
@users_router.get("/booking", summary="My current active booking (or null)")
async def my_booking(user: Principal = Depends(require_student), db: AsyncSession = Depends(get_db)):
    row = (await db.execute(
        select(Booking, Seat.seat_code).join(Seat, Seat.id == Booking.seat_id)
        .where(Booking.user_id == user.user_id, Booking.status.in_(ACTIVE)))).first()
    return booking_out(row[0], row[1]) if row else None


@users_router.get("/history", summary="My personal study history")
async def my_history(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                     user: Principal = Depends(require_student), db: AsyncSession = Depends(get_db)):
    base = Booking.user_id == user.user_id
    total = (await db.execute(select(func.count()).select_from(Booking).where(base))).scalar_one()
    rows = (await db.execute(
        select(Booking, Seat.seat_code).join(Seat, Seat.id == Booking.seat_id).where(base)
        .order_by(Booking.created_at.desc()).offset((page - 1) * page_size).limit(page_size))).all()
    mins = (await db.execute(select(func.coalesce(func.sum(Booking.actual_duration_mins), 0)).where(
        base, Booking.checked_in_at.is_not(None)))).scalar_one()
    return {"page": page, "page_size": page_size, "total": total, "total_minutes_studied": mins,
            "items": [booking_out(b, code) for b, code in rows]}


# ------------------------------------------------------------------ websocket
@ws_router.websocket("/ws/seats")
async def ws_seats(ws: WebSocket, token: str = Query("")):
    """Live floor map. Connect with ws://host/ws/seats?token=<JWT>.
    Sends SNAPSHOT on connect, then SEAT_UPDATE per change; students also get RENEWAL_WINDOW_OPEN."""
    async with SessionLocal() as db:
        principal = await principal_from_token(token, db) if token else None
    if principal is None:
        await ws.close(code=4401)
        return
    await manager.connect(ws, principal.user_id)
    try:
        await ws.send_json(await snapshot_message())
        while True:
            await ws.receive_text()          # keep-alive; client messages are ignored
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(ws)
