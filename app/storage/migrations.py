"""Small, transactional migrations; caller holds BEGIN IMMEDIATE and startup process lock."""
import logging
import time

import aiosqlite

from app.storage.phase2_schema import STATEMENTS
from app.storage.policy_schema import STATEMENTS as POLICY_STATEMENTS

logger = logging.getLogger(__name__)


async def migrate_self_id(connection: aiosqlite.Connection) -> None:
    async with connection.execute("PRAGMA table_info(summary_jobs)") as cursor:
        jobs_need_migration = "self_id" not in {row["name"] for row in await cursor.fetchall()}
    async with connection.execute("PRAGMA table_info(summaries)") as cursor:
        summaries_need_migration = "self_id" not in {row["name"] for row in await cursor.fetchall()}
    if jobs_need_migration:
        # Nullable only for historical rows whose account cannot be established.
        await connection.execute("ALTER TABLE summary_jobs ADD COLUMN self_id INTEGER CHECK(self_id > 0)")
        async with connection.execute(
            "SELECT value FROM runtime_state WHERE key='onebot_self_id'"
        ) as cursor:
            row = await cursor.fetchone()
        value = row["value"] if row else ""
        if value.isascii() and value.isdecimal() and len(value) <= 19 and 0 < int(value) <= 2**63 - 1:
            await connection.execute("UPDATE summary_jobs SET self_id=?", (int(value),))
        else:
            await connection.execute(
                "UPDATE summary_jobs SET status='failed',completed_at=?,"
                "error='missing self_id after schema migration' WHERE status IN ('queued','running')",
                (time.time(),))
        logger.info("Migrated summary_jobs self_id")
    if summaries_need_migration:
        await connection.execute("ALTER TABLE summaries ADD COLUMN self_id INTEGER CHECK(self_id > 0)")
        await connection.execute(
            "UPDATE summaries SET self_id=(SELECT self_id FROM summary_jobs WHERE id=summaries.job_id)")
        logger.info("Migrated summaries self_id")
    # Do not reassign unresolved legacy rows when an account is bound on a later startup.
    await connection.execute(
        "CREATE INDEX IF NOT EXISTS messages_self_window ON messages(self_id,group_id,event_time,id)")
    # ALTER TABLE cannot add NOT NULL without inventing a legacy default. Enforce it for new rows.
    for table in ("summary_jobs", "summaries"):
        await connection.execute(f"""
            CREATE TRIGGER IF NOT EXISTS {table}_require_self_id
            BEFORE INSERT ON {table} WHEN NEW.self_id IS NULL
            BEGIN SELECT RAISE(ABORT, 'self_id is required'); END
        """)


async def migrate_phase2(connection: aiosqlite.Connection) -> None:
    for statement in (*STATEMENTS, *POLICY_STATEMENTS):
        await connection.execute(statement)
    async with connection.execute("PRAGMA table_info(private_outbox)") as cursor:
        columns = {row["name"] for row in await cursor.fetchall()}
    for name, definition in {
        "kind": "TEXT NOT NULL DEFAULT 'text'", "self_id": "INTEGER",
        "attachment_id": "INTEGER REFERENCES attachments(id)", "cancelled_at": "REAL",
    }.items():
        if name not in columns:
            await connection.execute(f"ALTER TABLE private_outbox ADD COLUMN {name} {definition}")
