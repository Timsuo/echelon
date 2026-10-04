import sqlite3

import aiosqlite
import pytest

from app.storage.db import Database
from tests.test_triage_migration import phase25_database


async def phase4_database(path, monkeypatch):
    phase25_database(path)
    async def noop(connection):
        pass
    with monkeypatch.context() as patch:
        patch.setattr('app.storage.db.migrate_operational', noop)
        db = Database(path)
        try:
            await db.open()
        finally:
            await db.close()
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO private_outbox(text,created_at) VALUES ('legacy critical text is not priority evidence',1)")
        connection.execute("INSERT INTO configuration_proposals(self_id,admin_qq,intent_json,created_at,expires_at) VALUES (88,99,'{}',1,2)")
        names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='sqlite_sequence'")]
        columns = {table: [r[1] for r in connection.execute(f'PRAGMA table_info({table})')] for table in names}
        before = {table: connection.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall() for table in names}
        return columns, before


async def test_operational_migration_preserves_all_phase4_data_and_nulls(tmp_path, monkeypatch):
    path = tmp_path/'phase4.db'
    columns, before = await phase4_database(path, monkeypatch)
    for _ in range(2):
        db = Database(path)
        try:
            await db.open()
        finally:
            await db.close()
        with sqlite3.connect(path) as connection:
            for table, fields in columns.items():
                assert connection.execute(f"SELECT {','.join(fields)} FROM {table} ORDER BY rowid").fetchall() == before[table]
            assert connection.execute('SELECT COUNT(*) FROM private_outbox WHERE self_id IS NULL').fetchone()[0] > 0
            assert connection.execute('SELECT COUNT(*) FROM private_outbox WHERE priority!=50').fetchone()[0] == 0
            assert connection.execute('SELECT COUNT(*) FROM configuration_proposals WHERE source_request_id IS NOT NULL').fetchone()[0] == 0
            assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
            assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


@pytest.mark.parametrize('statement', ['ALTER TABLE private_outbox ADD COLUMN priority', 'ALTER TABLE configuration_proposals ADD COLUMN source_request_id',
    'ALTER TABLE digest_runs ADD COLUMN failed_at', 'CREATE INDEX IF NOT EXISTS outbox_ready', 'CREATE TABLE IF NOT EXISTS worker_health', 'CREATE VIEW IF NOT EXISTS outbox_status'])
async def test_operational_migration_rollback(statement, tmp_path, monkeypatch):
    path = tmp_path/'rollback.db'
    columns, before = await phase4_database(path, monkeypatch)
    execute = aiosqlite.Connection.execute
    def fail(connection, sql, *args, **kwargs):
        if statement in sql:
            raise RuntimeError('migration interruption')
        return execute(connection, sql, *args, **kwargs)
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
            assert [r[1] for r in connection.execute(f'PRAGMA table_info({table})')] == columns[table]
            assert connection.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall() == before[table]
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='worker_health'").fetchone() is None
