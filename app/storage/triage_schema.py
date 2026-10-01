"""Additive Phase 3 migration, executed in the existing startup transaction."""
import time

STATEMENTS = (
    "CREATE UNIQUE INDEX IF NOT EXISTS messages_identity ON messages(id,self_id,group_id)",
    """CREATE TABLE IF NOT EXISTS triage_jobs (
        id INTEGER PRIMARY KEY, self_id INTEGER NOT NULL, group_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','completed','failed')),
        window_start REAL NOT NULL, window_end REAL NOT NULL, created_at REAL NOT NULL,
        not_before REAL NOT NULL, started_at REAL, completed_at REAL,
        retry_count INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
        error TEXT, result_json TEXT, source_kind TEXT NOT NULL CHECK(source_kind IN ('realtime','history_recovery','mixed')),
        UNIQUE(id,self_id,group_id))""",
    "CREATE INDEX IF NOT EXISTS triage_queue ON triage_jobs(self_id,status,not_before,id)",
    """CREATE TABLE IF NOT EXISTS triage_message_state (
        message_id INTEGER PRIMARY KEY, self_id INTEGER NOT NULL, group_id INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','queued','processed','ignored','failed')),
        triage_job_id INTEGER, created_at REAL NOT NULL,
        FOREIGN KEY(message_id,self_id,group_id) REFERENCES messages(id,self_id,group_id),
        FOREIGN KEY(triage_job_id,self_id,group_id) REFERENCES triage_jobs(id,self_id,group_id))""",
    "CREATE INDEX IF NOT EXISTS triage_pending ON triage_message_state(self_id,status,group_id,message_id)",
    """CREATE TABLE IF NOT EXISTS triage_job_messages (
        job_id INTEGER NOT NULL, message_id INTEGER NOT NULL UNIQUE,
        self_id INTEGER NOT NULL, group_id INTEGER NOT NULL, PRIMARY KEY(job_id,message_id),
        FOREIGN KEY(message_id,self_id,group_id) REFERENCES messages(id,self_id,group_id),
        FOREIGN KEY(job_id,self_id,group_id) REFERENCES triage_jobs(id,self_id,group_id))""",
    """CREATE TABLE IF NOT EXISTS triage_preferences (
        self_id INTEGER PRIMARY KEY, preferences_json TEXT NOT NULL, updated_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS inbox_item_labels (
        inbox_item_id INTEGER NOT NULL, self_id INTEGER NOT NULL, label TEXT NOT NULL,
        PRIMARY KEY(inbox_item_id,label), FOREIGN KEY(inbox_item_id,self_id) REFERENCES inbox_items(id,self_id))""",
    "CREATE TABLE IF NOT EXISTS triage_settings (id INTEGER PRIMARY KEY CHECK(id=1), triage_start_at REAL NOT NULL)",
)


async def migrate_triage(connection) -> None:
    for table, additions in {
        "inbox_items": {"revision": "INTEGER NOT NULL DEFAULT 1", "model_priority": "TEXT",
                        "action_required": "INTEGER NOT NULL DEFAULT 0", "action_text": "TEXT",
                        "triaged_at": "REAL", "triage_model": "TEXT",
                        "coverage_status": "TEXT NOT NULL DEFAULT 'not_checked'"},
        "configuration_proposals": {"kind": "TEXT NOT NULL DEFAULT 'group_policy'"},
        "configuration_requests": {"kind": "TEXT NOT NULL DEFAULT 'group_policy'"},
    }.items():
        async with connection.execute(f"PRAGMA table_info({table})") as cursor:
            columns = {row['name'] for row in await cursor.fetchall()}
        for name, definition in additions.items():
            if name not in columns:
                await connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    for statement in STATEMENTS:
        await connection.execute(statement)
    # No SELECT-from-messages backfill: only new INSERTs acquire triage ownership.
    await connection.execute("INSERT INTO triage_settings VALUES (1,?) ON CONFLICT DO NOTHING", (time.time(),))
