"""Executed statement-by-statement inside the existing migration transaction."""
STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS attachments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, self_id INTEGER NOT NULL,
        group_id INTEGER NOT NULL, message_id INTEGER REFERENCES messages(id),
        file_id TEXT, filename TEXT NOT NULL, file_size INTEGER,
        busid INTEGER NOT NULL DEFAULT 0, source_type TEXT NOT NULL, source_key TEXT NOT NULL,
        local_path TEXT, sha256 TEXT, download_status TEXT NOT NULL
        CHECK(download_status IN ('pending','downloading','downloaded','failed','skipped')),
        error TEXT, attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
        created_at REAL NOT NULL, downloaded_at REAL,
        UNIQUE(self_id,group_id,file_id), UNIQUE(self_id,group_id,source_key), UNIQUE(id,self_id),
        FOREIGN KEY(message_id,self_id) REFERENCES messages(id,self_id)
    )""",
    "CREATE INDEX IF NOT EXISTS attachment_queue ON attachments(self_id,download_status,next_attempt,id)",
    """CREATE TABLE IF NOT EXISTS inbox_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, self_id INTEGER NOT NULL,
        title TEXT NOT NULL, summary TEXT, category TEXT, priority TEXT,
        source_group_id INTEGER, source_sender_id INTEGER, event_time INTEGER,
        deadline_at INTEGER, deadline_text TEXT, status TEXT NOT NULL DEFAULT 'unread',
        reason TEXT, confidence REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
        origin_key TEXT, UNIQUE(self_id,origin_key), UNIQUE(id,self_id)
    )""",
    "CREATE INDEX IF NOT EXISTS inbox_account_status ON inbox_items(self_id,status,id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS messages_id_self ON messages(id,self_id)",
    """CREATE TABLE IF NOT EXISTS inbox_item_messages (
        inbox_item_id INTEGER NOT NULL, message_id INTEGER NOT NULL, self_id INTEGER NOT NULL,
        PRIMARY KEY(inbox_item_id,message_id),
        FOREIGN KEY(inbox_item_id,self_id) REFERENCES inbox_items(id,self_id),
        FOREIGN KEY(message_id,self_id) REFERENCES messages(id,self_id)
    )""",
    """CREATE TABLE IF NOT EXISTS inbox_item_attachments (
        inbox_item_id INTEGER NOT NULL, attachment_id INTEGER NOT NULL, self_id INTEGER NOT NULL,
        PRIMARY KEY(inbox_item_id,attachment_id),
        FOREIGN KEY(inbox_item_id,self_id) REFERENCES inbox_items(id,self_id),
        FOREIGN KEY(attachment_id,self_id) REFERENCES attachments(id,self_id)
    )""",
]
