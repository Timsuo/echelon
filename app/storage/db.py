import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
from anyio import CancelScope

from app.operations.health import WorkerHealth
from app.storage.delivery_schema import migrate_delivery
from app.storage.migrations import migrate_history, migrate_phase2, migrate_self_id
from app.storage.operational_schema import migrate_operational
from app.storage.policy_schema import migrate_config_parser
from app.storage.schema import SCHEMA
from app.storage.triage_schema import migrate_triage

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()
        self.connection: aiosqlite.Connection | None = None
        self.delivery_wakeup = asyncio.Event()
        self.health = WorkerHealth(self)

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(self.path, isolation_level=None)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            PRAGMA foreign_keys=ON;
            PRAGMA busy_timeout=5000;
        """ + SCHEMA)
        async with self.transaction() as connection:
            await migrate_self_id(connection)
            await migrate_phase2(connection)
            await migrate_history(connection)
            await migrate_triage(connection)
            await migrate_delivery(connection)
            await migrate_operational(connection)
            await migrate_config_parser(connection)
        logger.info("DB initialized")

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        if self.connection is None:
            raise RuntimeError("Database not open")
        async with self.lock:
            try:
                # BEGIN itself can finish in SQLite's thread after its waiter is cancelled.
                await self.connection.execute("BEGIN IMMEDIATE")
                yield self.connection
                await self.connection.commit()
            except BaseException:
                # Starlette uses level cancellation; shield cleanup before releasing our lock.
                with CancelScope(shield=True):
                    await self.connection.rollback()
                logger.warning("DB transaction rolled back")
                raise

    async def close(self) -> None:
        if self.connection:
            await self.connection.close()
            self.connection = None
