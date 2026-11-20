# M4 — Smart Library Seat Finder (backend)

```bash
cp .env.example .env        # set SECRET_KEY, SMTP_*; for local testing add DEV_ECHO_OTP=true
docker compose up --build   # API + Swagger UI: http://localhost:8000/docs
```
SQLite lives in `./data/library.db` (bind-mounted to `/app/data`), WAL mode.

* Live map: `ws://localhost:8000/ws/seats?token=<JWT>` (SNAPSHOT on connect, SEAT_UPDATE per change, RENEWAL_WINDOW_OPEN to the student).
* Seed: 30 seats (F1-A01..F3-C10). Admin login comes only from `.env`.
* QR payload for each seat: `GET /api/admin/seats/qr-codes?include_png=true`.
* Concurrency test (server running): `python tests/test_concurrency.py --db data/library.db --cleanup`
  or `docker compose exec api python tests/test_concurrency.py --db /app/data/library.db --cleanup`.
* Run ONE uvicorn worker (WebSocket hub + background worker are in-process).
