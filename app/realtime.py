"""WebSocket hub for the live floor map."""
import asyncio

from fastapi import WebSocket

from .database import SessionLocal
from .services import group_by_floor, load_seat_views
from .models import utcnow
from .services import iso


class ConnectionManager:
    def __init__(self) -> None:
        self._conns: dict[WebSocket, int | None] = {}   # websocket -> student user_id (None for admin)

    @property
    def has_clients(self) -> bool:
        return bool(self._conns)

    async def connect(self, ws: WebSocket, user_id: int | None) -> None:
        await ws.accept()
        self._conns[ws] = user_id

    def disconnect(self, ws: WebSocket) -> None:
        self._conns.pop(ws, None)

    async def _send(self, ws: WebSocket, msg: dict) -> None:
        try:
            await asyncio.wait_for(ws.send_json(msg), timeout=2)
        except Exception:  # noqa: BLE001  (dead/slow client -> drop it)
            self.disconnect(ws)

    async def broadcast(self, msg: dict) -> None:
        await asyncio.gather(*(self._send(ws, msg) for ws in list(self._conns)))

    async def send_to_user(self, user_id: int, msg: dict) -> None:
        targets = [ws for ws, uid in self._conns.items() if uid == user_id]
        await asyncio.gather(*(self._send(ws, msg) for ws in targets))


manager = ConnectionManager()


async def snapshot_message() -> dict:
    async with SessionLocal() as db:
        views = await load_seat_views(db)   # public view: never includes student identity
    return {"type": "SNAPSHOT", "server_time": iso(utcnow()), "floors": group_by_floor(views)}


async def broadcast_seats(seat_ids) -> None:
    """Push fresh state for the given seats to every connected client (call AFTER commit)."""
    if not manager.has_clients or not seat_ids:
        return
    async with SessionLocal() as db:
        views = await load_seat_views(db, seat_ids)
    for v in views:
        await manager.broadcast({"type": "SEAT_UPDATE", "seat": v})


async def broadcast_snapshot() -> None:
    if manager.has_clients:
        await manager.broadcast(await snapshot_message())
