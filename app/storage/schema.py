SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY, self_id INTEGER NOT NULL, group_id INTEGER NOT NULL,
 message_id TEXT NOT NULL, user_id INTEGER NOT NULL, nickname TEXT NOT NULL,
 event_time INTEGER NOT NULL, received_time REAL NOT NULL, raw_message TEXT NOT NULL,
 normalized_text TEXT NOT NULL, reply_to_message_id TEXT, message_json TEXT NOT NULL,
 UNIQUE(self_id, group_id, message_id)
);
CREATE INDEX IF NOT EXISTS messages_window ON messages(group_id, event_time, id);
CREATE TABLE IF NOT EXISTS summary_jobs (
 id INTEGER PRIMARY KEY, self_id INTEGER NOT NULL CHECK(self_id > 0),
 group_id INTEGER NOT NULL, window_start REAL NOT NULL,
 window_end REAL NOT NULL, type TEXT NOT NULL DEFAULT 'summary',
 status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed')),
 retry_count INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
 started_at REAL, completed_at REAL, error TEXT
);
CREATE INDEX IF NOT EXISTS jobs_queue ON summary_jobs(status, id);
CREATE TABLE IF NOT EXISTS summaries (
 id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL UNIQUE REFERENCES summary_jobs(id),
 self_id INTEGER NOT NULL CHECK(self_id > 0),
 group_id INTEGER NOT NULL, window_start REAL NOT NULL, window_end REAL NOT NULL,
 model TEXT NOT NULL, source_message_count INTEGER NOT NULL,
 summary_json TEXT NOT NULL, rendered_text TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS command_receipts (
 self_id INTEGER NOT NULL, message_id TEXT NOT NULL, created_at REAL NOT NULL,
 PRIMARY KEY(self_id, message_id)
);
CREATE TABLE IF NOT EXISTS private_outbox (
 id INTEGER PRIMARY KEY, text TEXT NOT NULL, created_at REAL NOT NULL,
 sent_at REAL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
 error TEXT
);
"""
