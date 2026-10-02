"""Phase 4 additive schema; caller owns the migration transaction."""
import time

STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS group_authorizations (
        self_id INTEGER NOT NULL CHECK(self_id>0), group_id INTEGER NOT NULL CHECK(group_id>0),
        active INTEGER NOT NULL CHECK(active IN (0,1)), group_name TEXT,
        added_at REAL NOT NULL, removed_at REAL, updated_at REAL NOT NULL,
        PRIMARY KEY(self_id,group_id))""",
    "CREATE TABLE IF NOT EXISTS authorization_settings (self_id INTEGER PRIMARY KEY, bootstrapped_at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS delivery_preferences (self_id INTEGER PRIMARY KEY, preferences_json TEXT NOT NULL, updated_at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS delivery_settings (self_id INTEGER PRIMARY KEY, delivery_start_at REAL NOT NULL, last_heartbeat REAL, schedule_cursor REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS phase4_settings (id INTEGER PRIMARY KEY CHECK(id=1), started_at REAL NOT NULL)",
    """CREATE TABLE IF NOT EXISTS inbox_revision_events (
        id INTEGER PRIMARY KEY, self_id INTEGER NOT NULL, inbox_item_id INTEGER NOT NULL,
        revision INTEGER NOT NULL, triage_job_id INTEGER NOT NULL, group_id INTEGER NOT NULL,
        created_at REAL NOT NULL, UNIQUE(self_id,inbox_item_id,revision),
        FOREIGN KEY(inbox_item_id,self_id) REFERENCES inbox_items(id,self_id),
        FOREIGN KEY(triage_job_id,self_id,group_id) REFERENCES triage_jobs(id,self_id,group_id))""",
    """CREATE TABLE IF NOT EXISTS delivery_revision_state (
        self_id INTEGER NOT NULL, inbox_item_id INTEGER NOT NULL, revision INTEGER NOT NULL,
        fingerprint TEXT NOT NULL, evaluated_at REAL NOT NULL, decision TEXT NOT NULL,
        PRIMARY KEY(self_id,inbox_item_id,revision),
        FOREIGN KEY(inbox_item_id,self_id) REFERENCES inbox_items(id,self_id))""",
    """CREATE TABLE IF NOT EXISTS inbox_deliveries (
        id INTEGER PRIMARY KEY, self_id INTEGER NOT NULL, inbox_item_id INTEGER NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('urgent','urgent_update','recovery')),
        item_revision INTEGER NOT NULL, fingerprint TEXT NOT NULL, snapshot_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('queued','deferred','enqueued','delivered','cancelled')),
        scheduled_for REAL NOT NULL, created_at REAL NOT NULL, enqueued_at REAL, delivered_at REAL, cancelled_at REAL,
        UNIQUE(self_id,inbox_item_id,fingerprint), FOREIGN KEY(inbox_item_id,self_id) REFERENCES inbox_items(id,self_id))""",
    "CREATE INDEX IF NOT EXISTS deliveries_due ON inbox_deliveries(self_id,status,scheduled_for)",
    """CREATE TABLE IF NOT EXISTS digest_runs (
        id INTEGER PRIMARY KEY, self_id INTEGER NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('scheduled','manual','catchup')),
        scheduled_for REAL NOT NULL, window_start REAL NOT NULL, window_end REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','deferred','enqueued','delivered','skipped')),
        created_at REAL NOT NULL, enqueued_at REAL, delivered_at REAL, coverage_status TEXT, rendered_text TEXT,
        UNIQUE(self_id,scheduled_for,kind))""",
    """CREATE TABLE IF NOT EXISTS digest_run_items (
        digest_id INTEGER NOT NULL REFERENCES digest_runs(id), self_id INTEGER NOT NULL,
        inbox_item_id INTEGER NOT NULL, item_revision INTEGER NOT NULL, was_urgent INTEGER NOT NULL,
        snapshot_json TEXT NOT NULL, PRIMARY KEY(digest_id,inbox_item_id),
        FOREIGN KEY(inbox_item_id,self_id) REFERENCES inbox_items(id,self_id))""",
    "CREATE INDEX IF NOT EXISTS revision_events_window ON inbox_revision_events(self_id,created_at)",
)


async def migrate_delivery(connection) -> None:
    for statement in STATEMENTS:
        await connection.execute(statement)
    for table, additions in {
        'configuration_requests': {'target_group_id': 'INTEGER'},
        'triage_message_state': {'fast_track_at': 'REAL'},
        'private_outbox': {'producer_kind': 'TEXT', 'producer_key': 'TEXT', 'producer_chunk': 'INTEGER'},
        'summaries': {'schema_version': 'INTEGER NOT NULL DEFAULT 1', 'compact_json': 'TEXT', 'compact_rendered_text': 'TEXT'},
    }.items():
        async with connection.execute(f'PRAGMA table_info({table})') as cursor:
            columns = {row['name'] for row in await cursor.fetchall()}
        for name, definition in additions.items():
            if name not in columns:
                await connection.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
    await connection.execute("UPDATE configuration_requests SET target_group_id=group_id "
        "WHERE kind='group_policy' AND target_group_id IS NULL AND group_id>0")
    await connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS outbox_producer_chunk "
        "ON private_outbox(self_id,producer_kind,producer_key,producer_chunk) WHERE producer_key IS NOT NULL")
    await connection.execute("INSERT INTO phase4_settings VALUES (1,?) ON CONFLICT DO NOTHING", (time.time(),))
