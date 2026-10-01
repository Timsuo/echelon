import asyncio
import sqlite3
import time

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.application import create_app
from app.storage.db import Database
from app.storage.phase2_schema import STATEMENTS
from app.storage.policy_schema import STATEMENTS as POLICY_STATEMENTS
from tests.conftest import event
from tests.test_phase2_migration import phase1_database
from tests.test_server import credentials


def test_reconnect_backfill_does_not_block_live_events(tmp_path, config):
    app = create_app(config, credentials(), tmp_path)
    headers = {"Authorization": "Bearer test-token", "X-Self-ID": "88"}
    with TestClient(app) as client:
        with client.websocket_connect("/onebot/v11/ws", headers=headers) as ws:
            ws.send_json(event())
            ws.send_json(event(message_type="private", message_id=10, message="/status"))
            reply = ws.receive_json()
            ws.send_json({"echo": reply["echo"], "status": "ok", "retcode": 0})
        repository = app.state.services.repository
        gaps = client.portal.call(repository.query, "SELECT * FROM collection_gaps")
        assert len(gaps) == 1 and gaps[0]["ended_at"] is None
        with client.websocket_connect("/onebot/v11/ws", headers=headers) as ws:
            ws.send_json(event(message_type="private", message_id=11, message="/coverage"))
            reply = ws.receive_json()
            assert "Collection Coverage" in reply["params"]["message"][0]["data"]["text"]
            ws.send_json({"echo": reply["echo"], "status": "ok", "retcode": 0})
            jobs = client.portal.call(repository.query, "SELECT * FROM history_sync_jobs")
            assert len(jobs) == 1 and jobs[0]["mode"] == "reconnect"
            client.portal.call(repository.query, "UPDATE history_sync_jobs SET next_attempt=0")
            client.portal.call(repository.query, "UPDATE collection_gaps SET report_after=0")
            history_action = ws.receive_json()
            assert history_action["action"] == "get_group_msg_history"
            # Deliberately withhold history response; realtime collection and other commands continue.
            ws.send_json(event(message_id=2))
            ws.send_json(event(message_type="private", message_id=12, message="/status"))
            status = ws.receive_json()
            assert "数据库消息：\n2" in status["params"]["message"][0]["data"]["text"]
            ws.send_json({"echo": status["echo"], "status": "ok", "retcode": 0})
            ws.send_json({"echo": history_action["echo"], "status": "ok", "retcode": 0,
                          "data": {"messages": [None, event(message_id=3, time=int(time.time()) - 10)]}})
            report = ws.receive_json()
            assert "采集恢复报告" in report["params"]["message"][0]["data"]["text"]
            assert "PARTIAL" in report["params"]["message"][0]["data"]["text"]
            ws.send_json({"echo": report["echo"], "status": "ok", "retcode": 0})
            messages = client.portal.call(repository.query, "SELECT * FROM messages ORDER BY id")
            assert len(messages) == 3 and messages[-1]["ingest_source"] == "history_recovery"
    with sqlite3.connect(tmp_path / "data/messages.db") as connection:
        assert connection.execute("SELECT value FROM runtime_state WHERE key='service_clean_shutdown'").fetchone()[0] == "true"
        assert connection.execute("SELECT count(*) FROM history_sync_jobs WHERE status='running'").fetchone()[0] == 0


def phase2_database(path):
    phase1_database(path)
    with sqlite3.connect(path) as connection:
        for statement in (*STATEMENTS, *POLICY_STATEMENTS):
            connection.execute(statement)
        connection.execute("INSERT INTO attachments(self_id,group_id,message_id,file_id,filename,source_type,source_key,download_status,created_at) "
                           "VALUES (88,123,1,'f1','a.txt','message','file:f1','skipped',1)")
        connection.execute("INSERT INTO inbox_items(self_id,title,created_at,updated_at) VALUES (88,'original',1,1)")
        connection.execute("INSERT INTO inbox_item_messages VALUES (1,1,88)")
        connection.execute("INSERT INTO inbox_item_attachments VALUES (1,1,88)")
        connection.execute("INSERT INTO group_policies(self_id,group_id,mode,summary_enabled,inbox_enabled,attachment_download_enabled,"
                           "priority_watch_enabled,created_at,updated_at) VALUES (88,123,'inbox',1,1,1,0,1,1)")
        names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='sqlite_sequence'")]
        columns = {name: [row[1] for row in connection.execute(f"PRAGMA table_info({name})")] for name in names}
        values = {name: connection.execute(f"SELECT * FROM {name}").fetchall() for name in names}
    return columns, values


async def test_phase25_migration_preserves_phase2_data_and_is_idempotent(tmp_path):
    path = tmp_path / "messages.db"
    columns, values = phase2_database(path)
    for _ in range(2):
        database = Database(path)
        await database.open()
        await database.close()
        with sqlite3.connect(path) as connection:
            for name, original in values.items():
                assert connection.execute(f"SELECT {','.join(columns[name])} FROM {name}").fetchall() == original
            assert connection.execute("SELECT ingest_source FROM messages").fetchone()[0] == "realtime"
            for table in ("collection_gaps", "history_sync_jobs", "history_sync_state"):
                assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


async def test_history_migration_failure_rolls_back_field_and_tables(tmp_path, monkeypatch):
    path = tmp_path / "messages.db"
    phase2_database(path)
    original = aiosqlite.Connection.execute

    def fail(connection, sql, *args, **kwargs):
        if "CREATE TABLE IF NOT EXISTS history_sync_jobs" in sql:
            raise RuntimeError("migration interrupted")
        return original(connection, sql, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(aiosqlite.Connection, "execute", fail)
        database = Database(path)
        try:
            with pytest.raises(RuntimeError):
                await database.open()
        finally:
            await database.close()
    with sqlite3.connect(path) as connection:
        assert "ingest_source" not in {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='collection_gaps'").fetchone() is None
        assert connection.execute("SELECT title FROM inbox_items").fetchone()[0] == "original"


async def test_cancel_after_sqlite_begin_rolls_back(repository, monkeypatch):
    started = asyncio.Event()
    original = repository.db.connection.execute

    async def interrupted_begin():
        await original("BEGIN IMMEDIATE")
        started.set()
        await asyncio.Event().wait()

    def execute(sql, *args, **kwargs):
        return interrupted_begin() if sql == "BEGIN IMMEDIATE" else original(sql, *args, **kwargs)

    async def transaction():
        async with repository.db.transaction():
            pytest.fail("Cancelled before entering transaction body")

    with monkeypatch.context() as patch:
        patch.setattr(repository.db.connection, "execute", execute)
        task = asyncio.create_task(transaction())
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert (await repository.query("SELECT 1 AS n"))[0]["n"] == 1
