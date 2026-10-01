import sqlite3

import aiosqlite
import pytest

from app.storage.db import Database
from app.storage.history_schema import STATEMENTS
from tests.test_history_lifecycle import phase2_database


def phase25_database(path):
    phase2_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE messages ADD COLUMN ingest_source TEXT NOT NULL DEFAULT 'realtime'")
        for statement in STATEMENTS:
            connection.execute(statement)
        connection.execute("INSERT INTO collection_gaps(self_id,started_at,ended_at,reason,recovery_status,created_at,updated_at) VALUES (88,1,2,'test','unknown',1,2)")
        connection.execute("INSERT INTO configuration_proposals(self_id,admin_qq,intent_json,created_at,expires_at) VALUES (88,99,'{}',1,2)")
        tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='sqlite_sequence'")]
        columns = {t: [r[1] for r in connection.execute(f'PRAGMA table_info({t})')] for t in tables}
        values = {t: connection.execute(f'SELECT * FROM {t}').fetchall() for t in tables}
    return columns, values


async def test_phase25_upgrade_preserves_all_data_no_mass_triage_and_idempotent(tmp_path):
    path = tmp_path / 'messages.db'
    columns, values = phase25_database(path)
    cutoff = None
    for _ in range(2):
        db = Database(path)
        await db.open()
        await db.close()
        with sqlite3.connect(path) as connection:
            for table, original in values.items():
                assert connection.execute(f"SELECT {','.join(columns[table])} FROM {table}").fetchall() == original
            assert connection.execute('SELECT count(*) FROM triage_message_state').fetchone()[0] == 0
            assert connection.execute('SELECT revision FROM inbox_items').fetchone()[0] == 1
            assert connection.execute('SELECT kind FROM configuration_proposals').fetchone()[0] == 'group_policy'
            new_cutoff = connection.execute('SELECT triage_start_at FROM triage_settings').fetchone()[0]
            assert cutoff is None or cutoff == new_cutoff
            cutoff = new_cutoff


async def test_phase3_migration_failure_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / 'messages.db'
    phase25_database(path)
    original = aiosqlite.Connection.execute
    def fail(connection, sql, *args, **kwargs):
        if 'CREATE TABLE IF NOT EXISTS triage_preferences' in sql:
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
        assert 'revision' not in {r[1] for r in connection.execute('PRAGMA table_info(inbox_items)')}
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='triage_jobs'").fetchone() is None
        assert connection.execute('SELECT count(*) FROM messages').fetchone()[0] == 1
        assert connection.execute('SELECT count(*) FROM collection_gaps').fetchone()[0] == 1
