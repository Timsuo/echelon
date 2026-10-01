import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from app.storage.migrations import migrate_phase2, migrate_self_id
from app.storage.schema import SCHEMA

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()
        self.connection: aiosqlite.Connection | None = None

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
        logger.info("DB initialized")

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        if self.connection is None:
            raise RuntimeError("Database not open")
        async with self.lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
                await self.connection.commit()
            except BaseException:
                # Cancellation must also release SQLite's transaction lock.
                await self.connection.rollback()
                logger.warning("DB transaction rolled back")
                raise

    async def close(self) -> None:
        if self.connection:
            await self.connection.close()
            self.connection = None
