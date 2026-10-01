STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS collection_gaps (
        id INTEGER PRIMARY KEY AUTOINCREMENT, self_id INTEGER NOT NULL,
        started_at REAL NOT NULL, ended_at REAL, reason TEXT NOT NULL,
        recovery_status TEXT NOT NULL DEFAULT 'pending',
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        report_after REAL, reported_at REAL, UNIQUE(id,self_id)
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS gap_open ON collection_gaps(self_id) WHERE ended_at IS NULL",
    "CREATE INDEX IF NOT EXISTS gap_window ON collection_gaps(self_id,started_at,ended_at)",
    """CREATE TABLE IF NOT EXISTS history_sync_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, self_id INTEGER NOT NULL, group_id INTEGER NOT NULL,
        gap_id INTEGER, mode TEXT NOT NULL CHECK(mode IN ('reconnect','periodic','manual')),
        window_start REAL NOT NULL, window_end REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0, messages_received INTEGER NOT NULL DEFAULT 0,
        messages_inserted INTEGER NOT NULL DEFAULT 0, invalid_messages INTEGER NOT NULL DEFAULT 0,
        oldest_message_time INTEGER, newest_message_time INTEGER,
        coverage TEXT NOT NULL DEFAULT 'unknown', error TEXT,
        created_at REAL NOT NULL, started_at REAL, completed_at REAL,
        UNIQUE(gap_id,group_id), FOREIGN KEY(gap_id,self_id) REFERENCES collection_gaps(id,self_id)
    )""",
    "CREATE INDEX IF NOT EXISTS history_queue ON history_sync_jobs(self_id,status,next_attempt,id)",
    """CREATE UNIQUE INDEX IF NOT EXISTS history_active ON history_sync_jobs(self_id,group_id,mode)
        WHERE gap_id IS NULL AND status IN ('queued','running')""",
    """CREATE TABLE IF NOT EXISTS history_sync_state (
        self_id INTEGER NOT NULL, group_id INTEGER NOT NULL,
        last_scheduled_at REAL NOT NULL, last_history_sync_at REAL,
        last_history_sync_result TEXT, last_history_oldest_event INTEGER, last_history_newest_event INTEGER,
        last_periodic_at REAL, PRIMARY KEY(self_id,group_id)
    )""",
)
