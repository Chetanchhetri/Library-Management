import base64
import io
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_admin
from ..database import get_db
from ..models import AuditLog, Seat, utcnow
from ..services import iso

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])

ENDED = "('SESSION_COMPLETED','EARLY_VACATED')"


def _naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


async def _rows(db: AsyncSession, sql: str, **params) -> list[dict]:
    return [dict(r) for r in (await db.execute(text(sql), params)).mappings().all()]


# ------------------------------------------------------------------ top users
@router.get("/analytics/top-users", summary="Students ranked by total minutes studied")
async def top_users(limit: int = Query(20, ge=1, le=200), db: AsyncSession = Depends(get_db)):
    rows = await _rows(db, f"""
        SELECT u.id AS user_id, u.name, u.email,
               COALESCE(SUM(CASE WHEN b.status IN {ENDED} AND b.checked_in_at IS NOT NULL
                                 THEN b.actual_duration_mins END), 0)            AS total_minutes,
               SUM(CASE WHEN b.status = 'SESSION_COMPLETED' THEN 1 ELSE 0 END)  AS completed_sessions,
               ROUND(AVG(CASE WHEN b.status IN {ENDED} AND b.checked_in_at IS NOT NULL
                              THEN b.actual_duration_mins END), 1)              AS avg_session_mins,
               COALESCE(SUM(b.renewal_count), 0)                                AS total_renewals
        FROM users u JOIN bookings b ON b.user_id = u.id
        GROUP BY u.id ORDER BY total_minutes DESC, completed_sessions DESC LIMIT :limit""", limit=limit)
    return {"items": rows}


# ------------------------------------------------------------------ seat behaviour
@router.get("/analytics/seat-behavior",
            summary="Hogging/no-show flags, positive contributors, peak hours, seat utilization")
async def seat_behavior(
    days: int = Query(30, ge=1, le=3650, description="Look-back window"),
    min_no_shows: int = Query(2, ge=1),
    renewal_threshold: int = Query(3, ge=1, description="Renewals within ONE booking that count as excessive"),
    min_sessions: int = Query(3, ge=1, description="Min finished sessions for the early-vacate ranking"),
    tz_offset_minutes: int = Query(0, ge=-720, le=840, description="Shift peak-hour buckets from UTC (e.g. 330)"),
    db: AsyncSession = Depends(get_db),
):
    since = utcnow() - timedelta(days=days)

    no_show_users = await _rows(db, """
        SELECT u.id AS user_id, u.name, u.email, COUNT(*) AS no_shows,
               (SELECT COUNT(*) FROM bookings x WHERE x.user_id = u.id AND x.created_at >= :since) AS total_bookings
        FROM bookings b JOIN users u ON u.id = b.user_id
        WHERE b.status = 'NO_SHOW_TIMEOUT' AND b.created_at >= :since
        GROUP BY u.id HAVING COUNT(*) >= :n ORDER BY no_shows DESC""", since=since, n=min_no_shows)
    for r in no_show_users:
        r["no_show_rate"] = round(r["no_shows"] / r["total_bookings"], 3) if r["total_bookings"] else 0

    no_show_seats = await _rows(db, """
        SELECT s.id AS seat_id, s.seat_code, COUNT(*) AS no_shows
        FROM bookings b JOIN seats s ON s.id = b.seat_id
        WHERE b.status = 'NO_SHOW_TIMEOUT' AND b.created_at >= :since
        GROUP BY s.id HAVING COUNT(*) >= :n ORDER BY no_shows DESC""", since=since, n=min_no_shows)

    excessive_renewals = await _rows(db, """
        SELECT u.id AS user_id, u.name, u.email, SUM(b.renewal_count) AS total_renewals,
               MAX(b.renewal_count) AS max_renewals_in_one_session, COUNT(*) AS bookings
        FROM bookings b JOIN users u ON u.id = b.user_id
        WHERE b.created_at >= :since
        GROUP BY u.id HAVING MAX(b.renewal_count) >= :thr
        ORDER BY max_renewals_in_one_session DESC, total_renewals DESC""", since=since, thr=renewal_threshold)

    contributors = await _rows(db, f"""
        SELECT u.id AS user_id, u.name, u.email,
               SUM(CASE WHEN b.status = 'EARLY_VACATED' THEN 1 ELSE 0 END) AS early_vacates,
               COUNT(*) AS finished_sessions
        FROM bookings b JOIN users u ON u.id = b.user_id
        WHERE b.status IN {ENDED} AND b.created_at >= :since
        GROUP BY u.id HAVING COUNT(*) >= :m""", since=since, m=min_sessions)
    for r in contributors:
        r["early_vacate_rate"] = round(r["early_vacates"] / r["finished_sessions"], 3)
    contributors = [r for r in contributors if r["early_vacates"] > 0]
    contributors.sort(key=lambda r: (-r["early_vacate_rate"], -r["early_vacates"]))

    modifier = f"{tz_offset_minutes:+d} minutes"
    hourly = {r["hour"]: r["bookings"] for r in await _rows(db, """
        SELECT CAST(strftime('%H', created_at, :mod) AS INTEGER) AS hour, COUNT(*) AS bookings
        FROM bookings WHERE created_at >= :since GROUP BY hour""", since=since, mod=modifier)}
    peak_hours = [{"hour": h, "bookings": hourly.get(h, 0)} for h in range(24)]

    seats = await _rows(db, """
        SELECT s.id AS seat_id, s.seat_code, s.floor, COUNT(b.id) AS bookings,
               COALESCE(SUM(CASE WHEN b.checked_in_at IS NOT NULL THEN b.actual_duration_mins END), 0) AS total_minutes,
               COALESCE(SUM(b.renewal_count), 0) AS renewals,
               COALESCE(SUM(CASE WHEN b.status = 'NO_SHOW_TIMEOUT' THEN 1 ELSE 0 END), 0) AS no_shows
        FROM seats s LEFT JOIN bookings b ON b.seat_id = s.id AND b.created_at >= :since
        GROUP BY s.id""", since=since)
    used = [s for s in seats if s["total_minutes"] > 0]
    mean = sum(s["total_minutes"] for s in used) / len(used) if used else 0
    for s in seats:
        s["avg_session_mins"] = round(s["total_minutes"] / max(1, s["bookings"] - s["no_shows"]), 1)
        s["hogged"] = bool(mean) and s["total_minutes"] > 1.5 * mean
    ranked = sorted(seats, key=lambda s: (-s["total_minutes"], -s["bookings"]))
    least = sorted(seats, key=lambda s: (s["total_minutes"], s["bookings"]))

    return {
        "window_days": days,
        "hogging_no_show_flags": {
            "no_show_users": no_show_users,
            "no_show_seats": no_show_seats,
            "excessive_renewal_users": excessive_renewals,
        },
        "positive_contributors": contributors,
        "peak_hours": peak_hours,
        "seat_utilization": {
            "most_used": ranked[:5],
            "least_used": least[:5],
            "always_hogged_seats": [s for s in ranked if s["hogged"]],
            "all_seats": ranked,
        },
    }


# ------------------------------------------------------------------ audit logs
@router.get("/logs", summary="Paginated, filterable audit log")
async def logs(
    user_id: int | None = None, seat_id: int | None = None, action_type: str | None = None,
    date_from: datetime | None = None, date_to: datetime | None = None,
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    conds = []
    if user_id is not None:
        conds.append(AuditLog.user_id == user_id)
    if seat_id is not None:
        conds.append(AuditLog.seat_id == seat_id)
    if action_type:
        conds.append(AuditLog.action_type == action_type.upper())
    if date_from:
        conds.append(AuditLog.timestamp >= _naive_utc(date_from))
    if date_to:
        conds.append(AuditLog.timestamp <= _naive_utc(date_to))

    total = (await db.execute(select(func.count()).select_from(AuditLog).where(*conds))).scalar_one()
    rows = (await db.execute(select(AuditLog).where(*conds).order_by(AuditLog.timestamp.desc(), AuditLog.id.desc())
                             .offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {
        "page": page, "page_size": page_size, "total": total,
        "items": [{"id": r.id, "user_id": r.user_id, "seat_id": r.seat_id, "booking_id": r.booking_id,
                   "action_type": r.action_type, "timestamp": iso(r.timestamp),
                   "details": json.loads(r.details_json or "{}")} for r in rows],
    }


# ------------------------------------------------------------------ QR codes
@router.get("/seats/qr-codes", summary="Seat QR tokens (optionally base64 PNGs) for printing/testing")
async def qr_codes(include_png: bool = False, db: AsyncSession = Depends(get_db)):
    seats = (await db.execute(select(Seat).order_by(Seat.floor, Seat.seat_code))).scalars().all()
    out = []
    for s in seats:
        item = {"seat_id": s.id, "seat_code": s.seat_code, "qr_secret_token": s.qr_secret_token,
                "qr_payload": f"libseat://{s.id}/{s.qr_secret_token}"}
        if include_png:
            import qrcode  # lazy import

            buf = io.BytesIO()
            qrcode.make(item["qr_payload"]).save(buf, format="PNG")
            item["qr_png_base64"] = base64.b64encode(buf.getvalue()).decode()
        out.append(item)
    return {"items": out}
