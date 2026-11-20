"""Background worker: no-show auto-release, 5-minute renewal notice, session auto-complete."""
import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select

from .config import settings
from .database import write_tx
from .models import Booking, Seat, utcnow
from .realtime import broadcast_seats, broadcast_snapshot, manager
from .services import finish_booking, iso, log_event

logger = logging.getLogger("worker")


async def run_cycle() -> None:
    now = utcnow()
    changed: set[int] = set()
    notices: list[tuple[int, dict]] = []

    async with write_tx() as db:
        # 1) PENDING_CHECKIN past its deadline -> NO_SHOW_TIMEOUT
        stale = (await db.execute(select(Booking).where(
            Booking.status == "PENDING_CHECKIN", Booking.checkin_deadline < now))).scalars().all()
        for b in stale:
            seat = await db.get(Seat, b.seat_id)
            finish_booking(seat, b, "NO_SHOW_TIMEOUT", now)
            log_event(db, "NO_SHOW_TIMEOUT", user_id=b.user_id, seat_id=b.seat_id, booking_id=b.id,
                      details={"checkin_deadline": iso(b.checkin_deadline)})
            changed.add(seat.id)

        # 2) OCCUPIED past session end -> SESSION_COMPLETED
        done = (await db.execute(select(Booking).where(
            Booking.status == "OCCUPIED", Booking.session_ends_at <= now))).scalars().all()
        for b in done:
            seat = await db.get(Seat, b.seat_id)
            finish_booking(seat, b, "SESSION_COMPLETED", b.session_ends_at)
            log_event(db, "SESSION_COMPLETED", user_id=b.user_id, seat_id=b.seat_id, booking_id=b.id,
                      details={"actual_duration_mins": b.actual_duration_mins, "renewals": b.renewal_count})
            changed.add(seat.id)

        # 3) Entering the final 5 minutes -> one-time renewal notification
        window_end = now + timedelta(minutes=settings.RENEWAL_WINDOW_MINUTES)
        soon = (await db.execute(select(Booking).where(
            Booking.status == "OCCUPIED", Booking.renewal_notified == False,  # noqa: E712
            Booking.session_ends_at > now, Booking.session_ends_at <= window_end))).scalars().all()
        for b in soon:
            seat = await db.get(Seat, b.seat_id)
            b.renewal_notified = True
            notices.append((b.user_id, {
                "type": "RENEWAL_WINDOW_OPEN", "seat_id": seat.id, "seat_code": seat.seat_code,
                "session_ends_at": iso(b.session_ends_at),
                "seconds_remaining": int((b.session_ends_at - now).total_seconds()),
            }))

    await broadcast_seats(changed)
    for user_id, msg in notices:
        await manager.send_to_user(user_id, msg)


async def worker_loop() -> None:
    tick = 0
    while True:
        try:
            await run_cycle()
            tick += 1
            if tick % 6 == 0:                 # ~30 s: resync clients' countdowns
                await broadcast_snapshot()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("worker cycle failed")
        await asyncio.sleep(settings.WORKER_INTERVAL_SECONDS)
