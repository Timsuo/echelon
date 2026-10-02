import sqlite3

import aiosqlite
import pytest

from app.storage.db import Database
from app.storage.repository import Repository
from tests.test_triage_migration import phase25_database


async def phase3_database(path, monkeypatch):
    phase25_database(path)
    async def no_phase4(connection):
        pass
    with monkeypatch.context() as patch:
        patch.setattr('app.storage.db.migrate_delivery', no_phase4)
        db = Database(path)
        await db.open()
        await db.close()
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO configuration_requests(self_id,admin_qq,group_id,message_id,input_text,created_at,kind) VALUES (88,99,0,'prefs','test',1,'triage_preferences')")
        tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='sqlite_sequence'")]
        columns = {t: [r[1] for r in connection.execute(f'PRAGMA table_info({t})')] for t in tables}
        values = {t: connection.execute(f'SELECT * FROM {t}').fetchall() for t in tables}
    return columns, values


async def test_phase3_upgrade_preserves_real_schema_and_all_rows(tmp_path, monkeypatch):
    path = tmp_path / 'phase3.db'
    columns, values = await phase3_database(path, monkeypatch)
    start = None
    for _ in range(2):
        db = Database(path)
        await db.open()
        await db.close()
        with sqlite3.connect(path) as connection:
            for table in columns:
                assert connection.execute(f"SELECT {','.join(columns[table])} FROM {table}").fetchall() == values[table]
            assert connection.execute('SELECT COUNT(*) FROM group_authorizations').fetchone()[0] == 0
            assert connection.execute('SELECT COUNT(*) FROM inbox_revision_events').fetchone()[0] == 0
            assert connection.execute('SELECT schema_version FROM summaries').fetchone()[0] == 1
            assert connection.execute("SELECT target_group_id FROM configuration_requests WHERE kind='triage_preferences'").fetchone()[0] is None
            current = connection.execute('SELECT started_at FROM phase4_settings').fetchone()[0]
            assert start is None or start == current
            start = current
    db = Database(path)
    await db.open()
    try:
        repo = Repository(db, authorization_seed=[123])
        assert await repo.bind_onebot(88)
        assert await repo.authorizations.list_active(88) == [123]
    finally:
        await db.close()


@pytest.mark.parametrize('failure', ['CREATE TABLE IF NOT EXISTS inbox_deliveries', 'ALTER TABLE summaries ADD COLUMN compact_json', 'CREATE UNIQUE INDEX IF NOT EXISTS outbox_producer_chunk'])
async def test_phase4_migration_transaction_rollback(failure, tmp_path, monkeypatch):
    path = tmp_path / 'phase3.db'
    columns, values = await phase3_database(path, monkeypatch)
    original = aiosqlite.Connection.execute
    def fail(connection, sql, *args, **kwargs):
        if failure in sql:
            raise RuntimeError('injected migration failure')
        return original(connection, sql, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(aiosqlite.Connection, 'execute', fail)
        db = Database(path)
        try:
            with pytest.raises(RuntimeError):
                await db.open()
        finally:
            await db.close()
    with sqlite3.connect(path) as connection:
        for table in columns:
            assert connection.execute(f'SELECT * FROM {table}').fetchall() == values[table]
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='group_authorizations'").fetchone() is None
