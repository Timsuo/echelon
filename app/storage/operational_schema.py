"""Additive Phase 4.5 migration. NULL outbox accounts belong only to legacy rows.

Do not guess their account or regroup old unkeyed chunks. Failure overlay columns
avoid rebuilding delivery tables and their provenance foreign keys.
"""


async def migrate_operational(connection):
    for table, additions in {
        'private_outbox': {'priority': 'INTEGER NOT NULL DEFAULT 50', 'dead_letter_at': 'REAL',
                          'terminal_attempts': 'INTEGER NOT NULL DEFAULT 0'},
        'configuration_proposals': {'source_request_id': 'INTEGER REFERENCES configuration_requests(id)'},
        'inbox_deliveries': {'failed_at': 'REAL', 'failure_reason': 'TEXT'},
        'digest_runs': {'failed_at': 'REAL', 'failure_reason': 'TEXT'},
    }.items():
        async with connection.execute(f'PRAGMA table_info({table})') as cursor:
            columns = {row['name'] for row in await cursor.fetchall()}
        for name, definition in additions.items():
            if name not in columns:
                await connection.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
    statements = (
        'CREATE UNIQUE INDEX IF NOT EXISTS proposal_source_request ON configuration_proposals(self_id,kind,source_request_id) WHERE source_request_id IS NOT NULL',
        'CREATE INDEX IF NOT EXISTS outbox_ready ON private_outbox(self_id,next_attempt,id) WHERE sent_at IS NULL AND cancelled_at IS NULL AND dead_letter_at IS NULL',
        'CREATE INDEX IF NOT EXISTS outbox_predecessor ON private_outbox(self_id,producer_kind,producer_key,producer_chunk,id) WHERE sent_at IS NULL',
        'CREATE INDEX IF NOT EXISTS outbox_failed ON private_outbox(self_id,dead_letter_at DESC) WHERE dead_letter_at IS NOT NULL',
        'CREATE INDEX IF NOT EXISTS digest_pending ON digest_runs(self_id,kind,status,scheduled_for) WHERE status IN (\'queued\',\'deferred\')',
        'CREATE INDEX IF NOT EXISTS summary_health ON summary_jobs(self_id,status,created_at)',
        'CREATE TABLE IF NOT EXISTS worker_health (name TEXT PRIMARY KEY, expected INTEGER NOT NULL, started_at REAL NOT NULL, last_success REAL, stale_seconds REAL NOT NULL)',
        "CREATE VIEW IF NOT EXISTS inbox_delivery_status AS SELECT *,CASE WHEN failed_at IS NOT NULL THEN 'failed' ELSE status END AS effective_status FROM inbox_deliveries",
        "CREATE VIEW IF NOT EXISTS digest_run_status AS SELECT *,CASE WHEN failed_at IS NOT NULL THEN 'failed' ELSE status END AS effective_status FROM digest_runs",
        "CREATE VIEW IF NOT EXISTS outbox_status AS SELECT *,CASE WHEN dead_letter_at IS NOT NULL THEN 'dead_letter' WHEN cancelled_at IS NOT NULL THEN 'cancelled' WHEN sent_at IS NOT NULL THEN 'sent' WHEN attempts>0 THEN 'retrying' ELSE 'queued' END AS effective_status FROM private_outbox",
    )
    for statement in statements:
        await connection.execute(statement)
