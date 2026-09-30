import sqlite3

import aiosqlite
import pytest

from app.storage.db import Database
from app.storage.repository import Repository

# Actual pre-migration table layouts, independent of the current production schema.
LEGACY_SCHEMA = """
CREATE TABLE messages (
 id INTEGER PRIMARY KEY, self_id INTEGER NOT NULL, group_id INTEGER NOT NULL,
 message_id TEXT NOT NULL, user_id INTEGER NOT NULL, nickname TEXT NOT NULL,
 event_time INTEGER NOT NULL, received_time REAL NOT NULL, raw_message TEXT NOT NULL,
 normalized_text TEXT NOT NULL, reply_to_message_id TEXT, message_json TEXT NOT NULL,
 UNIQUE(self_id, group_id, message_id)
);
CREATE TABLE summary_jobs (
 id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL, window_start REAL NOT NULL,
 window_end REAL NOT NULL, type TEXT NOT NULL DEFAULT 'summary',
 status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed')),
 retry_count INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
 started_at REAL, completed_at REAL, error TEXT
);
CREATE TABLE summaries (
 id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL UNIQUE REFERENCES summary_jobs(id),
 group_id INTEGER NOT NULL, window_start REAL NOT NULL, window_end REAL NOT NULL,
 model TEXT NOT NULL, source_message_count INTEGER NOT NULL,
 summary_json TEXT NOT NULL, rendered_text TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE runtime_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO messages VALUES (1,88,123,'1',99,'Nick',1,1,'A','A',NULL,'{}');
INSERT INTO summary_jobs(id,group_id,window_start,window_end,status,created_at)
 VALUES (1,123,0,100,'completed',1), (2,123,0,100,'queued',1),
        (3,123,0,100,'running',1), (4,123,0,100,'failed',1);
INSERT INTO summaries VALUES (1,1,123,0,100,'legacy-model',1,'{}','legacy result',1);
"""


def legacy_database(path, bound_id):
    with sqlite3.connect(path) as connection:
        connection.executescript(LEGACY_SCHEMA)
        if bound_id is not None:
            connection.execute("INSERT INTO runtime_state VALUES ('onebot_self_id',?)", (bound_id,))


async def test_existing_database_migration_and_idempotence(tmp_path):
    path = tmp_path / "legacy.db"
    legacy_database(path, "88")
    database = Database(path)
    await database.open()
    repository = Repository(database)
    try:
        jobs = await repository.query("SELECT * FROM summary_jobs ORDER BY id")
        summaries = await repository.query("SELECT * FROM summaries")
        assert [row["self_id"] for row in jobs] == [88] * 4
        assert [row["status"] for row in jobs] == ["completed", "queued", "running", "failed"]
        assert summaries[0]["self_id"] == 88
        assert summaries[0]["rendered_text"] == "legacy result"
        assert (await repository.query("SELECT * FROM messages"))[0]["normalized_text"] == "A"
        await repository.state("onebot_self_id", "222")
        await database.close()
        await database.open()
        assert await repository.query("SELECT * FROM summary_jobs ORDER BY id") == jobs
        assert await repository.query("SELECT * FROM summaries") == summaries
        await repository.recover()
        assert (await repository.claim_job())["self_id"] == 88
        # Nullable legacy schema must still reject NEW rows without identity.
        with pytest.raises(aiosqlite.IntegrityError, match="self_id"):
            await repository.query("INSERT INTO summary_jobs(group_id,window_start,window_end,status,created_at) "
                                   "VALUES (123,0,1,'queued',0)")
    finally:
        await database.close()


@pytest.mark.parametrize("binding", [None, "garbage", "0", "9223372036854775808"])
async def test_unknown_legacy_account_fails_closed_and_stays_unassigned(tmp_path, binding):
    path = tmp_path / "legacy.db"
    legacy_database(path, binding)
    database = Database(path)
    await database.open()
    repository = Repository(database)
    try:
        jobs = await repository.query("SELECT * FROM summary_jobs ORDER BY id")
        assert all(row["self_id"] is None for row in jobs)
        assert [row["status"] for row in jobs] == ["completed", "failed", "failed", "failed"]
        assert all(row["error"] == "missing self_id after schema migration" for row in jobs[1:3])
        assert (await repository.query("SELECT self_id FROM summaries"))[0]["self_id"] is None
        await repository.recover()
        assert await repository.claim_job() is None
        await repository.state("onebot_self_id", "222")
        await database.close()
        await database.open()
        assert await repository.query("SELECT * FROM summary_jobs ORDER BY id") == jobs
        assert (await repository.query("SELECT self_id FROM summaries"))[0]["self_id"] is None
        assert (await repository.query("SELECT count(*) AS n FROM messages"))[0]["n"] == 1
    finally:
        await database.close()


async def test_migration_rolls_back_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    legacy_database(path, "88")
    database = Database(path)
    original_execute = aiosqlite.Connection.execute

    def fail_second_alter(connection, sql, *args, **kwargs):
        if sql.startswith("ALTER TABLE summaries"):
            raise RuntimeError("simulated migration failure")
        return original_execute(connection, sql, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(aiosqlite.Connection, "execute", fail_second_alter)
        try:
            with pytest.raises(RuntimeError, match="simulated migration failure"):
                await database.open()
        finally:
            await database.close()
    with sqlite3.connect(path) as connection:
        assert "self_id" not in {row[1] for row in connection.execute("PRAGMA table_info(summary_jobs)")}
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
    await database.open()
    try:
        rows = await Repository(database).query("SELECT self_id FROM summary_jobs")
        assert all(row["self_id"] == 88 for row in rows)
    finally:
        await database.close()
