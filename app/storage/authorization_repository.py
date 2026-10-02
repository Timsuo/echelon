"""Runtime authorization. Configuration is a one-time seed, never an allow decision."""
import time


async def is_active(connection, self_id: int, group_id: int) -> bool:
    async with connection.execute(
        "SELECT 1 FROM group_authorizations WHERE self_id=? AND group_id=? AND active=1",
        (self_id, group_id),
    ) as cursor:
        return await cursor.fetchone() is not None


async def active_version(connection, self_id: int, group_id: int) -> float | None:
    async with connection.execute('SELECT updated_at FROM group_authorizations WHERE self_id=? AND group_id=? AND active=1', (self_id, group_id)) as cursor:
        row = await cursor.fetchone()
        return row[0] if row else None


async def active_ids(connection, self_id: int) -> list[int]:
    async with connection.execute(
        "SELECT group_id FROM group_authorizations WHERE self_id=? AND active=1 ORDER BY group_id",
        (self_id,),
    ) as cursor:
        return [row[0] for row in await cursor.fetchall()]


async def bootstrap(connection, self_id: int, seed: tuple[int, ...]) -> None:
    now = time.time()
    async with connection.execute(
        "INSERT INTO authorization_settings VALUES (?,?) ON CONFLICT DO NOTHING RETURNING self_id",
        (self_id, now),
    ) as cursor:
        if await cursor.fetchone() is None:
            return
    for group_id in seed:
        await connection.execute(
            "INSERT INTO group_authorizations(self_id,group_id,active,added_at,updated_at) "
            "VALUES (?,?,1,?,?) ON CONFLICT DO NOTHING", (self_id, group_id, now, now))


async def activate(connection, self_id: int, group_id: int, name: str | None = None) -> None:
    now = time.time()
    await connection.execute(
        "INSERT INTO group_authorizations(self_id,group_id,active,group_name,added_at,updated_at) "
        "VALUES (?,?,1,?,?,?) ON CONFLICT(self_id,group_id) DO UPDATE SET active=1,"
        "group_name=COALESCE(excluded.group_name,group_name),removed_at=NULL,updated_at=excluded.updated_at",
        (self_id, group_id, name, now, now))


async def deactivate(connection, self_id: int, group_id: int) -> None:
    now = time.time()
    await connection.execute("UPDATE group_authorizations SET active=0,removed_at=?,updated_at=? "
                             "WHERE self_id=? AND group_id=?", (now, now, self_id, group_id))
    for table in ('triage_jobs', 'history_sync_jobs', 'summary_jobs'):
        await connection.execute(f"UPDATE {table} SET status='failed',error='authorization_removed',completed_at=? "
            "WHERE self_id=? AND group_id=? AND status IN ('queued','running')", (now, self_id, group_id))
    await connection.execute("UPDATE history_sync_jobs SET coverage='failed' WHERE self_id=? AND group_id=? AND error='authorization_removed'", (self_id, group_id))
    # Keep aggregate recovery reports from waiting forever on a cancelled job.
    from app.storage.history_repository import HistoryRepository, rows
    gaps = await rows(connection, 'SELECT DISTINCT gap_id FROM history_sync_jobs WHERE self_id=? AND group_id=? AND gap_id IS NOT NULL', (self_id, group_id))
    for gap in gaps:
        await HistoryRepository._aggregate(connection, gap['gap_id'], now)
    await connection.execute("UPDATE triage_message_state SET status='failed' WHERE self_id=? AND group_id=? "
                             "AND status IN ('pending','queued')", (self_id, group_id))
    await connection.execute("UPDATE attachments SET download_status='skipped',error='authorization_removed' "
        "WHERE self_id=? AND group_id=? AND download_status IN ('pending','downloading')", (self_id, group_id))
    await connection.execute("UPDATE inbox_deliveries SET status='cancelled',cancelled_at=? WHERE self_id=? "
        "AND inbox_item_id IN (SELECT id FROM inbox_items WHERE self_id=? AND source_group_id=?) "
        "AND status IN ('queued','deferred')", (now, self_id, self_id, group_id))


class GroupAuthorizationRepository:
    def __init__(self, db):
        self.db = db

    async def is_active(self, self_id: int, group_id: int) -> bool:
        async with self.db.transaction() as connection:
            return await is_active(connection, self_id, group_id)

    async def version(self, self_id: int, group_id: int) -> float | None:
        async with self.db.transaction() as connection:
            return await active_version(connection, self_id, group_id)

    async def list_active(self, self_id: int) -> list[int]:
        async with self.db.transaction() as connection:
            return await active_ids(connection, self_id)

    async def list_known(self, self_id: int) -> list[dict]:
        async with self.db.transaction() as connection:
            async with connection.execute("SELECT * FROM group_authorizations WHERE self_id=? ORDER BY active DESC,group_id",
                                          (self_id,)) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def bootstrap(self, self_id: int, seed) -> None:
        async with self.db.transaction() as connection:
            async with connection.execute("SELECT value FROM runtime_state WHERE key='onebot_self_id'") as cursor:
                bound = await cursor.fetchone()
            if not bound or bound[0] != str(self_id):
                raise ValueError('Authorization bootstrap requires bound account')
            await bootstrap(connection, self_id, tuple(seed))

    async def activate(self, self_id: int, group_id: int, name: str | None = None) -> None:
        async with self.db.transaction() as connection:
            await activate(connection, self_id, group_id, name)

    async def deactivate(self, self_id: int, group_id: int) -> None:
        async with self.db.transaction() as connection:
            await deactivate(connection, self_id, group_id)
