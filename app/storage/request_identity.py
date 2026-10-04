"""Helpers used inside the caller's transaction; no separate receipt commit."""
import time


async def claim_command(connection, self_id, message_id):
    if message_id is None:
        return True  # Internal callers have no OneBot event.
    async with connection.execute('INSERT INTO command_receipts VALUES (?,?,?) ON CONFLICT DO NOTHING RETURNING message_id',
                                  (self_id, message_id, time.time())) as cursor:
        return await cursor.fetchone() is not None


async def request_running(connection, self_id, admin_qq, kind, request_id):
    if request_id is None:
        return True
    async with connection.execute('SELECT 1 FROM configuration_proposals WHERE self_id=? AND kind=? AND source_request_id=?',
                                  (self_id, kind, request_id)) as cursor:
        existing = await cursor.fetchone()
    if existing:
        await connection.execute("UPDATE configuration_requests SET status='completed' WHERE id=? AND self_id=? AND admin_qq=? AND kind=? AND status='running'",
                                 (request_id, self_id, admin_qq, kind))
        return False
    async with connection.execute("SELECT 1 FROM configuration_requests WHERE id=? AND self_id=? AND admin_qq=? AND kind=? AND status='running'",
                                  (request_id, self_id, admin_qq, kind)) as cursor:
        return await cursor.fetchone() is not None


async def request_exists(connection, self_id, message_id):
    async with connection.execute('SELECT 1 FROM configuration_requests WHERE self_id=? AND message_id=?', (self_id, message_id)) as cursor:
        return await cursor.fetchone() is not None
