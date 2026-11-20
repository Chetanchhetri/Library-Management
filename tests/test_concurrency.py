"""Fire N simultaneous booking requests at ONE seat; expect exactly 1x201 and (N-1)x409.

Run against a live server (host):    python tests/test_concurrency.py --db data/library.db
Run inside the container:            docker compose exec api python tests/test_concurrency.py --db /app/data/library.db

Test students (loadtestN@library.edu) are inserted straight into the DB as verified users,
because real signup needs an emailed OTP. Use --cleanup to delete them afterwards.
Needs: httpx, bcrypt.
"""
import argparse
import asyncio
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone

import bcrypt
import httpx

PASSWORD = "LoadTest-Passw0rd!"
EMAIL = "loadtest{}@library.edu"


def seed_users(db_path: str, n: int) -> None:
    pw_hash = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt()).decode()
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    con = sqlite3.connect(db_path, timeout=10)
    con.execute("PRAGMA busy_timeout=5000")
    with con:
        for i in range(n):
            con.execute(
                "INSERT INTO users (name, email, password_hash, is_verified, created_at) VALUES (?,?,?,1,?) "
                "ON CONFLICT(email) DO UPDATE SET password_hash=excluded.password_hash, is_verified=1",
                (f"Load Tester {i}", EMAIL.format(i), pw_hash, now))
    con.close()


def cleanup(db_path: str) -> None:
    con = sqlite3.connect(db_path, timeout=10)
    con.execute("PRAGMA busy_timeout=5000")
    with con:
        ids = [r[0] for r in con.execute("SELECT id FROM users WHERE email LIKE 'loadtest%@library.edu'")]
        for uid in ids:
            con.execute("UPDATE seats SET status='AVAILABLE', current_booking_id=NULL WHERE current_booking_id IN "
                        "(SELECT id FROM bookings WHERE user_id=?)", (uid,))
            con.execute("DELETE FROM audit_logs WHERE user_id=?", (uid,))
            con.execute("DELETE FROM bookings WHERE user_id=?", (uid,))
            con.execute("DELETE FROM users WHERE id=?", (uid,))
    con.close()
    print(f"cleaned up {len(ids)} test users")


async def main(args) -> int:
    seed_users(args.db, args.users)
    limits = httpx.Limits(max_connections=args.users + 10)
    async with httpx.AsyncClient(base_url=args.base_url, timeout=60, limits=limits) as client:
        async def login(i: int) -> dict:
            r = await client.post("/api/auth/login", json={"email": EMAIL.format(i), "password": PASSWORD})
            r.raise_for_status()
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        headers = await asyncio.gather(*(login(i) for i in range(args.users)))

        seats = (await client.get("/api/seats", headers=headers[0])).json()["floors"]
        seat = next(s for fl in seats.values() for s in fl if s["seat_code"] == args.seat_code)
        if seat["status"] != "AVAILABLE":
            print(f"Seat {args.seat_code} is {seat['status']}; pick another with --seat-code")
            return 2

        go = asyncio.Event()

        async def attempt(h):
            await go.wait()
            return await client.post(f"/api/seats/{seat['id']}/book", json={"duration_minutes": 60}, headers=h)

        tasks = [asyncio.create_task(attempt(h)) for h in headers]
        await asyncio.sleep(0.5)
        go.set()                                   # release all requests at once
        results = await asyncio.gather(*tasks, return_exceptions=True)

        codes = Counter(r.status_code if isinstance(r, httpx.Response) else repr(r) for r in results)
        print(f"{args.users} simultaneous bookings on {args.seat_code}: {dict(codes)}")

        winners = [i for i, r in enumerate(results) if isinstance(r, httpx.Response) and r.status_code == 201]
        con = sqlite3.connect(args.db, timeout=10)
        active = con.execute("SELECT COUNT(*) FROM bookings WHERE seat_id=? AND status IN "
                             "('PENDING_CHECKIN','OCCUPIED')", (seat["id"],)).fetchone()[0]
        con.close()

        ok = codes.get(201, 0) == 1 and codes.get(409, 0) == args.users - 1 and active == 1
        print("PASS: exactly one booking, zero double-bookings" if ok else
              f"FAIL: expected 1x201 + {args.users - 1}x409 and 1 active booking (db active={active})")

        for i in winners:                          # free the seat again
            await client.post(f"/api/seats/{seat['id']}/vacate", headers=headers[i])

    if args.cleanup:
        cleanup(args.db)
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--db", default="data/library.db", help="path to the SQLite file the server uses")
    ap.add_argument("--users", type=int, default=30)
    ap.add_argument("--seat-code", default="F1-A01")
    ap.add_argument("--cleanup", action="store_true", help="delete test users/bookings afterwards")
    sys.exit(asyncio.run(main(ap.parse_args())))
