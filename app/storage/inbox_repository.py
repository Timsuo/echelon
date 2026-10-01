import logging
import time

from app.attachments.models import GroupFileReference
from app.config import AttachmentConfig
from app.policies.models import GroupPolicy
from app.storage.db import Database

logger = logging.getLogger(__name__)


async def ingest_files(connection, record: dict, files: list[GroupFileReference],
                       policy: AttachmentConfig, group_policy: GroupPolicy) -> None:
    async with connection.execute(
        "SELECT id FROM messages WHERE self_id=? AND group_id=? AND message_id=?",
        (record["self_id"], record["group_id"], record["message_id"]),
    ) as cursor:
        message_id = (await cursor.fetchone())["id"]
    for file in files:
        if (file.self_id, file.group_id, file.message_id) != (
                record["self_id"], record["group_id"], record["message_id"]):
            raise ValueError("Attachment identity does not match message")
        status, error = "pending", None
        if not group_policy.attachment_download_enabled:
            status, error = "skipped", "group_policy"
        elif not policy.auto_download:
            status, error = "skipped", "auto_download_disabled"
        elif file.file_size is not None and file.file_size > policy.max_auto_download_mb * 1024**2:
            status, error = "skipped", "size_limit"
        elif not file.file_id and not file.url:
            status, error = "failed", "file_reference_unavailable"
        now = time.time()
        async with connection.execute(
            "INSERT INTO attachments(self_id,group_id,message_id,file_id,filename,file_size,busid,"
            "source_type,source_key,download_status,error,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT DO NOTHING RETURNING id",
            (file.self_id, file.group_id, message_id, file.file_id, file.filename, file.file_size,
             file.busid, file.source_type, file.source_key, status, error, now),
        ) as cursor:
            created = await cursor.fetchone()
        async with connection.execute(
            "SELECT id FROM attachments WHERE self_id=? AND group_id=? AND source_key=?",
            (file.self_id, file.group_id, file.source_key),
        ) as cursor:
            attachment = await cursor.fetchone()
        attachment_id = attachment["id"]
        # Some NapCat versions report busid only in the companion upload notice.
        await connection.execute("UPDATE attachments SET busid=CASE WHEN busid=0 THEN ? ELSE busid END,"
            "file_size=COALESCE(file_size,?) WHERE id=?", (file.busid, file.file_size, attachment_id))
        if created:
            logger.info("Attachment discovered id=%s group=%s status=%s", attachment_id, file.group_id, status)
            logger.info("Attachment %s id=%s", "queued" if status == "pending" else "skipped", attachment_id)
        if not group_policy.inbox_enabled:
            continue
        async with connection.execute(
            "INSERT INTO inbox_items(self_id,title,summary,source_group_id,source_sender_id,event_time,"
            "created_at,updated_at,origin_key) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING RETURNING id",
            (file.self_id, "文件：" + file.filename, f"来自 {file.group_id} 的群文件", file.group_id,
             record["user_id"], record["event_time"], now, now, f"attachment:{attachment_id}"),
        ) as cursor:
            item_created = await cursor.fetchone()
        async with connection.execute(
            "SELECT id FROM inbox_items WHERE self_id=? AND origin_key=?",
            (file.self_id, f"attachment:{attachment_id}"),
        ) as cursor:
            item_id = (await cursor.fetchone())["id"]
        await connection.execute("INSERT INTO inbox_item_messages VALUES (?,?,?) ON CONFLICT DO NOTHING",
                                 (item_id, message_id, file.self_id))
        await connection.execute("INSERT INTO inbox_item_attachments VALUES (?,?,?) ON CONFLICT DO NOTHING",
                                 (item_id, attachment_id, file.self_id))
        if item_created:
            logger.info("Inbox item created id=%s", item_id)


class InboxRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def query(self, sql: str, args: tuple = ()) -> list[dict]:
        async with self.db.transaction() as connection:
            async with connection.execute(sql, args) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def attachment(self, self_id: int, attachment_id: int) -> dict | None:
        rows = await self.query("SELECT * FROM attachments WHERE self_id=? AND id=?", (self_id, attachment_id))
        return rows[0] if rows else None

    async def claim_attachment(self, self_id: int) -> dict | None:
        rows = await self.query(
            "UPDATE attachments SET download_status='downloading',attempts=attempts+1 WHERE id="
            "(SELECT id FROM attachments WHERE self_id=? AND download_status='pending' AND next_attempt<=? "
            "ORDER BY id LIMIT 1) AND download_status='pending' RETURNING *", (self_id, time.time()))
        return rows[0] if rows else None

    async def recover(self) -> None:
        await self.query("UPDATE attachments SET download_status='pending' WHERE download_status='downloading'")

    async def download_result(self, attachment: dict, status: str, error: str | None = None,
                              path: str | None = None, sha256: str | None = None,
                              size: int | None = None) -> None:
        await self.query(
            "UPDATE attachments SET download_status=?,error=?,local_path=?,sha256=?,downloaded_at=?,"
            "file_size=COALESCE(?,file_size),next_attempt=? WHERE self_id=? AND id=?",
            (status, error, path, sha256, time.time() if status == "downloaded" else None, size,
             time.time() + min(30, 2 ** attachment["attempts"]), attachment["self_id"], attachment["id"]))

    async def list_items(self, self_id: int, unread: bool, limit: int) -> list[dict]:
        condition = "status='unread'" if unread else "status!='archived'"
        return await self.query(f"SELECT * FROM inbox_items WHERE self_id=? AND {condition} ORDER BY id DESC LIMIT ?",
                                (self_id, limit))

    async def detail(self, self_id: int, item_id: int, mark_read: bool = False) -> dict | None:
        async with self.db.transaction() as connection:
            if mark_read:
                await connection.execute("UPDATE inbox_items SET status='read',updated_at=? "
                    "WHERE self_id=? AND id=? AND status='unread'", (time.time(), self_id, item_id))
            async with connection.execute("SELECT * FROM inbox_items WHERE self_id=? AND id=?",
                                           (self_id, item_id)) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None
            result = dict(row)
            async with connection.execute(
                "SELECT a.* FROM attachments a JOIN inbox_item_attachments l ON a.id=l.attachment_id "
                "AND a.self_id=l.self_id WHERE l.inbox_item_id=? AND l.self_id=? ORDER BY a.id",
                (item_id, self_id),
            ) as cursor:
                result["attachments"] = [dict(row) for row in await cursor.fetchall()]
            async with connection.execute(
                "SELECT count(*) FROM inbox_item_messages WHERE inbox_item_id=? AND self_id=?",
                (item_id, self_id),
            ) as cursor:
                result["message_count"] = (await cursor.fetchone())[0]
            return result

    async def archive(self, self_id: int, item_id: int) -> bool:
        return bool(await self.query("UPDATE inbox_items SET status='archived',updated_at=? "
            "WHERE self_id=? AND id=? RETURNING id", (time.time(), self_id, item_id)))

    async def unread_count(self, self_id: int) -> int:
        return (await self.query("SELECT count(*) AS n FROM inbox_items WHERE self_id=? AND status='unread'",
                                  (self_id,)))[0]["n"]

    async def create_item(self, self_id: int, title: str, summary: str,
                          message_ids: list[int], attachment_ids: list[int]) -> int:
        """Explicit aggregation boundary; composite foreign keys forbid cross-account links."""
        if not title.strip() or len(title) > 1200:
            raise ValueError("Invalid inbox title")
        async with self.db.transaction() as connection:
            now = time.time()
            async with connection.execute("INSERT INTO inbox_items(self_id,title,summary,created_at,updated_at) "
                "VALUES (?,?,?,?,?)", (self_id, title, summary, now, now)) as cursor:
                item_id = cursor.lastrowid
            for message_id in set(message_ids):
                await connection.execute("INSERT INTO inbox_item_messages VALUES (?,?,?)", (item_id, message_id, self_id))
            for attachment_id in set(attachment_ids):
                await connection.execute("INSERT INTO inbox_item_attachments VALUES (?,?,?)", (item_id, attachment_id, self_id))
            return item_id
