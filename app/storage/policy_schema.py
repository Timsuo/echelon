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


async def migrate_config_parser(connection) -> None:
    # NULL marks legacy full input; never guess whether a new body's first number is a target.
    async with connection.execute("PRAGMA table_info(configuration_requests)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    if "intent_text" not in columns:
        await connection.execute("ALTER TABLE configuration_requests ADD COLUMN intent_text TEXT")
