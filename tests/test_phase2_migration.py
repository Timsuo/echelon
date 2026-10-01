import sqlite3

import aiosqlite
import pytest

from app.storage.db import Database
from app.storage.repository import Repository
from app.storage.schema import SCHEMA


def phase1_database(path):
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        connection.execute("INSERT INTO messages VALUES (1,88,123,'m1',99,'Nick',1,1,'A','A',NULL,'{}')")
        connection.execute("INSERT INTO summary_jobs(id,self_id,group_id,window_start,window_end,status,created_at) VALUES (1,88,123,0,2,'completed',1)")
        connection.execute("INSERT INTO summaries VALUES (1,1,88,123,0,2,'test',1,'{}','original summary',1)")
        connection.execute("INSERT INTO runtime_state VALUES ('onebot_self_id','88')")
        connection.execute("INSERT INTO command_receipts VALUES (88,'cmd',1)")
        connection.execute("INSERT INTO private_outbox(text,created_at) VALUES ('pending result',1)")
        names = ["messages", "summary_jobs", "summaries", "runtime_state", "command_receipts", "private_outbox"]
        columns = {name: [row[1] for row in connection.execute(f"PRAGMA table_info({name})")] for name in names}
        values = {name: connection.execute(f"SELECT * FROM {name}").fetchall() for name in names}
    return columns, values


async def test_phase1_upgrade_preserves_every_existing_table_and_is_idempotent(tmp_path):
    path = tmp_path / "messages.db"
    columns, values = phase1_database(path)
    database = Database(path)
    for _ in range(2):
        await database.open()
        await database.close()
        with sqlite3.connect(path) as connection:
            for name, original in values.items():
                assert connection.execute(f"SELECT {','.join(columns[name])} FROM {name}").fetchall() == original
            for name in ("attachments", "inbox_items", "inbox_item_messages", "inbox_item_attachments",
                         "group_policies", "configuration_proposals", "configuration_requests"):
                assert connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0] == 0
    await database.open()
    try:
        assert (await Repository(database).next_notification())["text"] == "pending result"
    finally:
        await database.close()


async def test_phase2_migration_failure_rolls_back_new_tables(tmp_path, monkeypatch):
    path = tmp_path / "messages.db"
    phase1_database(path)
    original = aiosqlite.Connection.execute

    def fail(connection, sql, *args, **kwargs):
        if "CREATE TABLE IF NOT EXISTS inbox_items" in sql:
            raise RuntimeError("simulated phase2 migration failure")
        return original(connection, sql, *args, **kwargs)

    database = Database(path)
    with monkeypatch.context() as patch:
        patch.setattr(aiosqlite.Connection, "execute", fail)
        try:
            with pytest.raises(RuntimeError):
                await database.open()
        finally:
            await database.close()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='attachments'").fetchone() is None
        assert connection.execute("SELECT rendered_text FROM summaries").fetchone()[0] == "original summary"
    await database.open()
    await database.close()
