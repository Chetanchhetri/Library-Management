"""Async SQLite engine with WAL + BEGIN IMMEDIATE write transactions."""
import asyncio
import os
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings

_PREFIX = "sqlite+aiosqlite:///"
if settings.DATABASE_URL.startswith(_PREFIX):
    _path = settings.DATABASE_URL[len(_PREFIX):]
    if _path and _path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(_path)), exist_ok=True)

engine = create_async_engine(settings.DATABASE_URL, pool_size=20, max_overflow=20)


@event.listens_for(engine.sync_engine, "connect")
def _on_connect(dbapi_conn, _record):
    # Autocommit mode at the driver level: we issue BEGIN IMMEDIATE ourselves for writes
    # and reads run outside any transaction (WAL readers never block).
    dbapi_conn.isolation_level = None
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA busy_timeout=5000;")
    cur.execute("PRAGMA synchronous=NORMAL;")
    cur.execute("PRAGMA foreign_keys=ON;")
    cur.close()


SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


# In-process mutex: queues writers inside this process so they never even reach SQLite's
# busy-timeout. BEGIN IMMEDIATE remains the cross-process guarantee.
_write_lock = asyncio.Lock()


@asynccontextmanager
async def write_tx():
    """Atomic write transaction: takes SQLite's write lock up-front (BEGIN IMMEDIATE)."""
    async with _write_lock:
        async with SessionLocal() as session:
            try:
                await session.execute(text("BEGIN IMMEDIATE"))
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise


async def get_db():
    """Read-only session dependency (no transaction held open)."""
    async with SessionLocal() as session:
        yield session


async def init_db():
    from . import models  # noqa: F401  (register tables)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
