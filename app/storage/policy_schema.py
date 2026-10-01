STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS group_policies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        self_id INTEGER NOT NULL, group_id INTEGER NOT NULL, alias TEXT,
        mode TEXT NOT NULL, summary_enabled INTEGER NOT NULL,
        inbox_enabled INTEGER NOT NULL, attachment_download_enabled INTEGER NOT NULL,
        priority_watch_enabled INTEGER NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
        UNIQUE(self_id,group_id)
    )""",
    """CREATE TABLE IF NOT EXISTS configuration_proposals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, self_id INTEGER NOT NULL, admin_qq INTEGER NOT NULL,
        intent_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        created_at REAL NOT NULL, expires_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS configuration_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, self_id INTEGER NOT NULL, admin_qq INTEGER NOT NULL,
        group_id INTEGER NOT NULL, message_id TEXT NOT NULL, input_text TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued', retry_count INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL, error TEXT, UNIQUE(self_id,message_id)
    )""",
)
